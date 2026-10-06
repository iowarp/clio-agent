"""Prediction rendering + the sibling reason catalogs (#714 decomposition).

* :func:`_extract_tools_called` -- a finished prediction's tool-call trace, wire-shaped;
* :func:`_signature_prompt` -- a signature docstring for catalog display;
* the ambient-LM and workflow_state-schema reason catalogs (siblings of the
  stream-fallback catalog in :mod:`clio_agent.gact.runtime.capabilities`);
* re-exports of the stream-fallback ledger (:mod:`clio_agent.gact.stream_fallbacks`).

Live text and thinking stream through the LM token hooks
(:mod:`clio_agent.runtime.lm_activity`); the turn runs the agent module once.
"""

from __future__ import annotations

import inspect
from typing import TYPE_CHECKING, Any

from clio_agent.gact.evidence import _bounded_tool_call_result
from clio_agent.gact.stream_fallbacks import (
    peek_stream_fallback as _peek_stream_fallback,  # noqa: F401
)
from clio_agent.gact.stream_fallbacks import (
    pop_stream_fallback as _pop_stream_fallback,  # noqa: F401
)
from clio_agent.gact.stream_fallbacks import (
    pop_stream_fallback_notes as _pop_stream_fallback_notes,  # noqa: F401
)
from clio_agent.gact.stream_fallbacks import (
    record_stream_fallback as _record_stream_fallback,  # noqa: F401
)
from clio_agent.gact.stream_fallbacks import (
    stream_fallback_payload as _stream_fallback_payload,  # noqa: F401
)
from clio_agent.gact.stream_fallbacks import (
    stream_fallback_reasons as _stream_fallback_reasons,  # noqa: F401
)

if TYPE_CHECKING:
    from fastapi import FastAPI


# --- ambient-LM fallback reason catalog (per-expert-provider sweep, #818) ----
# A SIBLING of the stream_fallback catalog for the ambient ``dspy.settings.lm``
# call-site sweep: a runtime helper (token accounting / auto-compaction / usage
# rollup / reasoning capture / model-id probe) resolved the process boot-default
# LM because no per-profile ``dspy.context`` was bound. Deliberately kept OUT of
# ``_STREAM_FALLBACK_REASON_DEFINITIONS`` (and its client-facing
# ``x_clio_stream_fallback_reasons`` capability, an audited *closed set* of
# live-streaming fallbacks) so an unrelated provider-binding reason cannot break
# that contract. It follows the same typed, reject-unknowns pattern and is
# recorded per session in the ambient-LM ledger (``gact.runtime.ambient_lm``) so
# the miss stays queryable rather than a silent dependency on the global default.
_AMBIENT_LM_FALLBACK_REASON_DEFINITIONS: dict[str, dict[str, Any]] = {
    "ambient_lm_default": {
        "category": "provider_binding",
        "recovery_actions": ["bind_active_profile_context", "pass_explicit_lm"],
        "description": (
            "A runtime call site resolved the process boot-default LM because no "
            "per-profile dspy.context was active. The result is valid but "
            "attributed to the boot default, not an expert/main profile; bind the "
            "active profile's context (or pass an explicit LM) to remove the "
            "ambient dependency."
        ),
    },
}


def _ambient_lm_fallback_payload(reason: str, message: str = "") -> dict[str, Any]:
    """Build a structured, typed payload for an ambient boot-default LM read.

    Mirrors :func:`_stream_fallback_payload` (validate against a typed catalog,
    reject unknowns) for the ambient-LM sweep's dedicated reason catalog, so a
    miss records a queryable typed reason instead of a bare fallback."""

    definition = _AMBIENT_LM_FALLBACK_REASON_DEFINITIONS.get(reason)
    if definition is None:
        raise ValueError(f"Unknown ambient LM fallback reason: {reason}")
    payload: dict[str, Any] = {
        "reason": reason,
        **{
            key: (list(value) if isinstance(value, list) else value)
            for key, value in definition.items()
        },
    }
    if message:
        payload["message"] = message
    return payload


# --- workflow_state schema fallback reason catalog (#646/#648, Phase C) -------
# A SIBLING of the stream_fallback catalog for the pack-declared workflow_state
# vocabulary seam: a session whose active Agent Blueprint declares no
# ``workflow_state`` schema runs the typed-state engine with the GENERIC
# (presence-only) schema instead of a domain-typed one. Deliberately kept OUT of
# ``_STREAM_FALLBACK_REASON_DEFINITIONS`` (and its client-facing
# ``x_clio_stream_fallback_reasons`` capability, an audited *closed set* of
# live-streaming fallbacks) so an unrelated pack-declaration reason cannot break
# that contract. It follows the same typed, reject-unknowns pattern and is
# recorded per session in a dedicated bounded ledger
# (``app.state.workflow_schema_fallbacks``) so the generic degradation stays
# queryable rather than a silent downgrade.
_WORKFLOW_SCHEMA_FALLBACK_REASON_DEFINITIONS: dict[str, dict[str, Any]] = {
    "workflow_state_schema_absent": {
        "category": "pack_declaration",
        "recovery_actions": [
            "declare_workflow_state_schema_in_agent_md",
            "continue_with_generic_merge",
        ],
        "description": (
            "The session's active Agent Blueprint declares no workflow_state schema, "
            "so the typed-state engine ran with the generic presence-only schema "
            "(rank 0 everywhere, no artifact grounding, no domain scrub aliases). "
            "Legitimate generic behavior, recorded so the degradation is queryable "
            "rather than silent."
        ),
    },
}

# Cap the per-session ledger so a long-lived session cannot grow it without bound;
# consecutive same-message records are de-duplicated before this cap is consulted.
_MAX_WORKFLOW_SCHEMA_LEDGER_ENTRIES = 64


def _workflow_schema_fallback_payload(reason: str, message: str = "") -> dict[str, Any]:
    """Build a structured, typed payload for a workflow_state generic fallback.

    Mirrors :func:`_stream_fallback_payload` / :func:`_ambient_lm_fallback_payload`
    (validate against a typed catalog, reject unknowns) for the workflow_state
    schema seam's dedicated reason catalog, so a miss records a queryable typed
    reason instead of a silent generic downgrade."""

    definition = _WORKFLOW_SCHEMA_FALLBACK_REASON_DEFINITIONS.get(reason)
    if definition is None:
        raise ValueError(f"Unknown workflow_state schema fallback reason: {reason}")
    payload: dict[str, Any] = {
        "reason": reason,
        **{
            key: (list(value) if isinstance(value, list) else value)
            for key, value in definition.items()
        },
    }
    if message:
        payload["message"] = message
    return payload


def _workflow_schema_fallbacks(app: "FastAPI") -> dict[str, list[dict[str, Any]]]:
    """Return the per-session workflow_state schema fallback ledger, creating it on first use.

    Keyed by session id, each value a list of catalog payloads (most-recent last).
    A dedicated per-app ledger (never the single-slot streaming ledger the turn
    handler pops for its ``stream_fallback`` metadata) so the generic-schema
    degradation stays queryable after the fact."""

    ledger = getattr(app.state, "workflow_schema_fallbacks", None)
    if not isinstance(ledger, dict):
        ledger = {}
        app.state.workflow_schema_fallbacks = ledger
    return ledger


def _record_workflow_schema_fallback(
    app: "FastAPI",
    sid: str,
    reason: str,
    message: str = "",
) -> None:
    """Record a structured workflow_state generic-fallback reason for a session.

    Builds the reason from the dedicated sibling catalog (via
    :func:`_workflow_schema_fallback_payload`) and appends it to the per-app
    workflow-schema ledger so the miss is queryable. Consecutive same-message
    records for a session are collapsed, and the ledger is capped, mirroring the
    ambient-LM ledger, so a session cannot grow it without bound. A missing
    app/state or session id is a no-op (nothing to attribute)."""

    if app is None or getattr(app, "state", None) is None or not sid:
        return
    payload = _workflow_schema_fallback_payload(reason, message)
    entries = _workflow_schema_fallbacks(app).setdefault(sid, [])
    # Collapse consecutive same-message records so a re-resolution leaves ONE
    # queryable entry per (session, blueprint) rather than one per call.
    if not entries or entries[-1].get("message") != message:
        entries.append(payload)
    if len(entries) > _MAX_WORKFLOW_SCHEMA_LEDGER_ENTRIES:
        del entries[:-_MAX_WORKFLOW_SCHEMA_LEDGER_ENTRIES]


def _extract_tools_called(pred: Any) -> list[dict[str, Any]]:
    """Pull an agent prediction's tool-call trace into a wire-shaped
    list.

    The tier-2 experts expose their tool calls on
    ``pred.tools_called`` when the ReAct loop tracks them. Each
    entry is either a ``clio_agent.arc.schema.ToolCall`` (msgspec
    struct), a plain dict, or an object with attribute access —
    handle all three. Fields copied onto the wire when present:
    name, args, ok, duration_ms, cached. All optional.
    """

    raw = getattr(pred, "tools_called", None)
    if not raw:
        return []

    out: list[dict[str, Any]] = []
    for call in raw:
        row: dict[str, Any] = {}
        agent_trace_call = False
        if isinstance(call, dict):

            def get(key: str, default: Any = None, _src: Any = call) -> Any:
                return _src.get(key, default)
        else:
            # msgspec structs + DSPy trace records — attribute access.
            def get(key: str, default: Any = None, _src: Any = call) -> Any:
                return getattr(_src, key, default)

            agent_trace_call = (
                hasattr(call, "tool") and hasattr(call, "params") and hasattr(call, "result")
            )

        name = get("name") or get("tool") or ""
        if name:
            row["name"] = str(name)

        args = get("args")
        if args is None:
            args = get("arguments")
        if args is None:
            args = get("params")
        if args is not None:
            row["args"] = args

        status = get("status")
        if status is not None:
            row["ok"] = status not in {"failure", "error", "timeout"}
        elif get("ok") is not None:
            row["ok"] = bool(get("ok"))

        duration_ms = get("duration_ms")
        if duration_ms is not None:
            row["duration_ms"] = float(duration_ms)

        cached = get("cached")
        if cached is not None:
            row["cached"] = bool(cached)

        result = get("result")
        if result is not None:
            row["result"] = _bounded_tool_call_result(result)
            if "ok" not in row and agent_trace_call:
                row["ok"] = not (
                    (isinstance(result, dict) and "error" in result)
                    or (isinstance(result, str) and result.startswith("Error:"))
                )

        telemetry_source = get("telemetry_source") or (
            "agent_trace" if agent_trace_call else "posthoc_prediction"
        )
        row["telemetry_source"] = str(telemetry_source)

        if row:
            out.append(row)
    return out


def _signature_prompt(signature: Any) -> str:
    """Return a cleaned DSPy signature docstring for catalog display."""
    return inspect.cleandoc(getattr(signature, "__doc__", "") or "")
