"""Reading one terminal clio-relay task result down to the tool's own payload.

Owner module for the relay result contract shared by every curated JARVIS
dispatch (#1195): locating clio-relay's durable job envelope inside a terminal
``tasks/get`` reply, unwrapping it to the remote tool's structured result, and
converting relay's own delivery/dispatch failure evidence into typed errors.
Split out of ``jarvis_jobs`` so the dispatch owner keeps only dispatch logic.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from clio_agent.tools.relay_transport import (
    RELAY_INLINE_LIMIT_CODE,
    RELAY_RESULT_DELIVERY_SCHEMA,
    RelayInlineResultTooLargeError,
    RelayTransportContractError,
)

__all__ = [
    "JarvisJobError",
    "raise_inline_delivery_failure",
    "raise_remote_call_failure",
    "structured_payload",
]


class JarvisJobError(RelayTransportContractError):
    """A curated JARVIS dispatch or execution violated its durable contract."""


_STRUCTURED_RESULT_KEYS = ("structured_result", "structuredContent", "structured_content")
# "job_id" (F2/R1): relay's create-time receipt (the eager `completed_result`
# a terminal-at-birth dispatch promotes -- #1195/C1) is flat, carrying
# `job_id` as a top-level sibling of `mcp_result` rather than the nested
# `job`/`relay_queue` the lazy wait document uses. Both are relay's own
# bookkeeping, never the tool's result; recognizing either shape as an
# envelope is what makes both resolve through the same one-hop-deeper
# unwrap into `mcp_result.structured_result` instead of one of them being
# handed to the agent verbatim.
_RELAY_JOB_ENVELOPE_SIBLING_KEYS = ("job", "relay_queue", "job_id")
_REMOTE_MESSAGE_LIMIT = 4_000


def _is_relay_job_envelope(candidate: Mapping[str, Any]) -> bool:
    """Whether ``candidate`` is clio-relay's durable job record, not a tool result.

    A relay job envelope carries the job's own bookkeeping (``job`` /
    ``relay_queue`` / ``artifacts`` / ``scheduler`` / ...) alongside a nested
    ``mcp_result`` that holds the tool's real structured output one hop
    deeper. The JARVIS tool's own structured result never carries those
    relay-owned siblings, so their presence is the shape signal -- not a
    guess about field names inside the tool's own schema.
    """

    return "mcp_result" in candidate and any(
        key in candidate for key in _RELAY_JOB_ENVELOPE_SIBLING_KEYS
    )


def _envelope_candidates(result: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """Every place a relay job envelope can sit in one terminal task result.

    Three observed carriers, all shape-addressed: the result *is* the envelope;
    the envelope sits under a structured-result key of the result (a replayed
    ``tasks/get`` reply); or under a structured-result key of the result's own
    ``mcp_result`` hop. Membership is decided by :func:`_is_relay_job_envelope`,
    never by position alone.
    """

    seen: list[Mapping[str, Any]] = [result]
    hop = result.get("mcp_result")
    if isinstance(hop, Mapping):
        seen.append(hop)
    for holder in tuple(seen):
        for key in _STRUCTURED_RESULT_KEYS:
            nested = holder.get(key)
            if isinstance(nested, Mapping):
                seen.append(nested)
    return [candidate for candidate in seen if _is_relay_job_envelope(candidate)]


def _remote_failure_details(inner: Mapping[str, Any]) -> dict[str, Any] | None:
    """Relay's own evidence that the delivered MCP call failed, or ``None``.

    ``mcp_result`` is clio-relay's record of the dispatched ``tools/call``. It
    reports failure through fields relay owns -- a ``protocol_error`` string, a
    non-zero ``returncode``, or a ``protocol_result`` bearing MCP's ``isError``
    -- so this reads relay's contract, not JARVIS's own result schema. A healthy
    call carries ``protocol_error: null``, ``returncode: 0`` and omits
    ``protocol_result`` entirely, so no successful dispatch matches here.
    """

    protocol_error = inner.get("protocol_error")
    returncode = inner.get("returncode")
    protocol_result = inner.get("protocol_result")
    is_error = (
        protocol_result.get("isError") is True if isinstance(protocol_result, Mapping) else False
    )
    failed = (
        (isinstance(protocol_error, str) and bool(protocol_error))
        or (isinstance(returncode, int) and not isinstance(returncode, bool) and returncode != 0)
        or is_error
    )
    if not failed:
        return None
    return {
        "protocol_error": protocol_error if isinstance(protocol_error, str) else None,
        "returncode": returncode if isinstance(returncode, int) else None,
        "remote_message": _remote_message(protocol_result),
    }


def _remote_message(protocol_result: Any) -> str | None:
    """Join the remote tool's own error text out of its MCP content blocks."""

    if not isinstance(protocol_result, Mapping):
        return None
    blocks = protocol_result.get("content")
    if not isinstance(blocks, (list, tuple)):
        return None
    texts = [
        block["text"]
        for block in blocks
        if isinstance(block, Mapping) and isinstance(block.get("text"), str)
    ]
    if not texts:
        return None
    return "\n".join(texts)[:_REMOTE_MESSAGE_LIMIT]


def raise_remote_call_failure(tool_name: str, task_id: str, result: Mapping[str, Any]) -> None:
    """Fail with the remote tool's own reason when the delivered call errored.

    A relay task reaching ``completed`` only means the dispatch was *delivered*;
    the JARVIS call inside it can still have failed. Live evidence (#1195): a
    ``jarvis_get_execution`` carrying an unsupported artifact filter came back
    with ``job.state: failed`` and the remote pydantic rejection in
    ``mcp_result.protocol_result``, but this layer read only the task status,
    fell through to an envelope with no ``structured_result``, and reported
    ``jarvis_execution_identity_mismatch`` -- an error about the wrong thing
    that discarded the remote message telling the caller exactly what to fix.
    """

    for envelope in _envelope_candidates(result):
        inner = envelope.get("mcp_result")
        if not isinstance(inner, Mapping):
            continue
        details = _remote_failure_details(inner)
        if details is None:
            continue
        job = envelope.get("job")
        raise JarvisJobError(
            f"{tool_name} reached JARVIS but the remote call failed",
            reason="jarvis_remote_call_failed",
            details={
                "tool": tool_name,
                "task_id": task_id,
                "job_state": job.get("state") if isinstance(job, Mapping) else None,
                **details,
            },
        )


def _structured_from_envelope(envelope: Mapping[str, Any]) -> dict[str, Any] | None:
    """Descend one hop into a relay job envelope's own ``mcp_result``.

    Returns ``None`` when the envelope's ``mcp_result`` is absent or does not
    itself carry a structured-result key -- the caller must not fall back to
    returning the envelope in that case, so :func:`structured_payload` raises
    its own typed ``jarvis_result_unwrap_failed`` instead of a malformed
    payload being masked or handed to a downstream check that reports the
    wrong thing (F2/N2/N3).
    """

    inner = envelope.get("mcp_result")
    if not isinstance(inner, Mapping):
        return None
    for key in _STRUCTURED_RESULT_KEYS:
        structured = inner.get(key)
        if isinstance(structured, Mapping):
            return dict(structured)
    return None


def structured_payload(
    result: Mapping[str, Any],
    *,
    tool_name: str = "",
    task_id: str = "",
) -> dict[str, Any]:
    """Unwrap relay terminal evidence to the JARVIS tool's structured result.

    Two shapes reach here. A **direct** payload already carries the tool's
    own fields at this level, or one ``mcp_result`` hop down, under a
    ``structured_result``/``structuredContent``/``structured_content`` key.
    A **relay job envelope** happens to satisfy that same key match --
    ``structuredContent`` also names the field the ``tasks/get`` transport
    itself uses to carry clio-relay's whole durable job record when a
    dispatch resolves through a resumed/replayed record -- so matching the
    key alone silently returns relay's bookkeeping instead of the tool's
    result (observed live: ``jarvis_get_execution`` reading
    ``pipeline_id: None`` off the envelope and failing its identity check).
    An envelope match is detected by shape (:func:`_is_relay_job_envelope`)
    and unwrapped one further hop into its own ``mcp_result``.

    If that hop comes up empty (D15: a relay envelope was positively
    identified -- ``mcp_result`` plus a job-identifying sibling -- but
    nothing inside it names the tool's structured result), this raises a
    typed ``jarvis_result_unwrap_failed`` for EVERY caller -- unconditionally,
    never gated by ``tool_name`` (F2/N2/N3). An earlier version carved
    ``jarvis_get_execution`` (and, unreachably, ``jarvis_run``) out of this
    raise on the theory that ``_execution_projection``'s own
    ``jarvis_execution_identity_mismatch`` was "more specific" for that
    caller. It was ruled unsound and removed: that downstream check reports
    a mismatch that never happened (``observed_*: null`` -- nothing was
    returned, nothing was compared) and is the exact bug
    :func:`raise_remote_call_failure`'s docstring already describes fixing
    for the sibling failure case. A caller wanting a more specific reason
    for this exact input must earn it with a test that compares the two
    reasons on the same envelope, not reuse one that never reaches this
    branch.

    A shape with no relay envelope signal at all (no ``mcp_result``
    anywhere) is never touched by this rule -- it is returned unchanged
    either way, since there is nothing here to say it is not already the
    tool's own final, already-resolved payload.
    """

    candidates: list[Mapping[str, Any]] = [result]
    mcp_result = result.get("mcp_result")
    if isinstance(mcp_result, Mapping):
        candidates.insert(0, mcp_result)
    envelope_detected = False
    for candidate in candidates:
        for key in _STRUCTURED_RESULT_KEYS:
            structured = candidate.get(key)
            if not isinstance(structured, Mapping):
                continue
            if _is_relay_job_envelope(structured):
                envelope_detected = True
                nested = _structured_from_envelope(structured)
                if nested is not None:
                    return nested
                continue
            return dict(structured)
    if envelope_detected:
        raise JarvisJobError(
            f"{tool_name} reached a relay job envelope with no reachable "
            "structured result",
            reason="jarvis_result_unwrap_failed",
            details={
                "tool": tool_name or None,
                "task_id": task_id or None,
                "observed_keys": sorted(result.keys()),
            },
        )
    return dict(result)


def raise_inline_delivery_failure(task_id: str, value: Any) -> None:
    """Preserve relay's typed oversized-result failure through the owner layer."""

    stack = [value]
    visited = 0
    while stack:
        current = stack.pop()
        visited += 1
        if visited > 100_000:
            raise JarvisJobError(
                "JARVIS dispatch result exceeded the validation node bound",
                reason="jarvis_dispatch_result_too_complex",
                details={"task_id": task_id, "max_nodes": 100_000},
            )
        if isinstance(current, Mapping):
            delivery = current.get("delivery")
            if (
                isinstance(delivery, Mapping)
                and delivery.get("schema_version") == RELAY_RESULT_DELIVERY_SCHEMA
                and delivery.get("status") == "failed"
                and delivery.get("code") == RELAY_INLINE_LIMIT_CODE
            ):
                raise RelayInlineResultTooLargeError(task_id, delivery)
            stack.extend(current.values())
        elif isinstance(current, (list, tuple)):
            stack.extend(current)
