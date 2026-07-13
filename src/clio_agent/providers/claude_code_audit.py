"""Stream-audit instrumentation for the Claude Code provider (#891).

The waterfall diagnosis (#891) needs two per-LM-call facts the existing
``stream_audit`` rows never carried:

* **when a prompt was submitted to the SDK** — the request-start marker that
  makes time-to-first-token (TTFT) and per-call wall duration measurable, plus
  a stable prompt-prefix fingerprint so consecutive calls' prefix stability
  (the driver of prompt-cache hits) is observable *without* persisting prompts.
* **the raw SDK usage payload** — ``input_tokens`` / ``output_tokens`` and, most
  importantly, ``cache_read_input_tokens`` / ``cache_creation_input_tokens``.
  These cache fields are collapsed into a single ``prompt_tokens`` sum by the
  LiteLLM ``ModelResponse`` bridge, so they are only visible here, before the
  bridge flattens them.

Both emitters are gated by the *existing* ``CLIO_STREAM_AUDIT_LOG`` switch and
return before doing any work (hashing, id lookup) when the audit is off, so the
inference path pays zero overhead in normal operation. The rows join to the
existing ``provider.raw_event`` chunk rows by ``call_index`` (which those rows
already carry); ``call_id`` additionally pairs a call's ``call_started`` with
its ``call_usage`` across processes, where ``call_index`` resets.
"""

from __future__ import annotations

import hashlib
from typing import Any

from clio_agent.runtime.stream_audit import stream_audit, stream_audit_enabled

# Number of leading prompt characters hashed into each prefix fingerprint. The
# two window sizes let the analyzer distinguish a short shared system-prompt
# prefix (2 KB) from a longer shared trajectory prefix (16 KB): a cache hit
# needs byte-stable prefixes, so equal fingerprints across consecutive calls are
# a necessary (not sufficient) condition for a prompt-cache read.
_PREFIX_SMALL_CHARS = 2048
_PREFIX_LARGE_CHARS = 16384


def active_gact_ids() -> tuple[str, str, str]:
    """Return active GACT session, turn, and trace ids for audit rows.

    Returns:
        A ``(session_id, turn_id, trace_id)`` triple, each empty when no GACT
        turn context is active (CLI / optimizer paths) or the lookup fails.
    """

    try:
        from clio_agent.gact.context import (  # noqa: PLC0415
            active_session_id,
            active_trace_id,
            active_turn_id,
        )

        return active_session_id(), active_turn_id(), active_trace_id()
    except Exception:  # noqa: BLE001 - provider audit must never break calls
        return "", "", ""


def prompt_prefix_fingerprint(prompt: str) -> tuple[str, str]:
    """Return ``(sha256_first_2k_chars, sha256_first_16k_chars)`` of ``prompt``.

    The hashes stand in for the prompt itself so prefix stability across calls
    is measurable without storing any prompt text. Hashing is over the leading
    characters (not bytes) of the UTF-8 prompt; identical returned hashes mean
    the two prompts share a byte-identical leading window of that size.

    Args:
        prompt: The fully serialized prompt submitted to the SDK.

    Returns:
        A ``(small, large)`` pair of hex SHA-256 digests over the first
        :data:`_PREFIX_SMALL_CHARS` and :data:`_PREFIX_LARGE_CHARS` characters.
    """

    small = hashlib.sha256(prompt[:_PREFIX_SMALL_CHARS].encode("utf-8", "replace")).hexdigest()
    large = hashlib.sha256(prompt[:_PREFIX_LARGE_CHARS].encode("utf-8", "replace")).hexdigest()
    return small, large


def _provider_label(transport: str) -> str:
    """Map a transport name to the provider label used across audit rows."""

    return "claude_code_sdk" if transport == "sdk" else "claude_code_exec"


def emit_call_started(
    *, call_id: str, call_index: int, model: str, transport: str, prompt: str
) -> None:
    """Emit a ``provider.call_started`` audit row at prompt-submission time.

    This is the request-start marker: its timestamp minus the first
    ``provider.raw_event`` timestamp for the same ``call_index`` yields TTFT, and
    minus the paired ``provider.call_usage`` timestamp yields the call's wall
    duration. No-op (and free) when stream audit is disabled.

    Args:
        call_id: Per-call correlation id pairing this row with its usage row.
        call_index: Process-local monotonic call index (also on raw_event rows).
        model: The clean model name (SDK ``--model`` value).
        transport: ``"sdk"`` or ``"exec"`` — selects the provider label.
        prompt: The serialized prompt being submitted (only its length and a
            prefix fingerprint are recorded, never the text).
    """

    if not stream_audit_enabled():
        return
    session_id, turn_id, trace_id = active_gact_ids()
    prefix_small, prefix_large = prompt_prefix_fingerprint(prompt)
    stream_audit(
        "provider.call_started",
        provider=_provider_label(transport),
        call_id=call_id,
        call_index=call_index,
        session_id=session_id,
        turn_id=turn_id,
        trace_id=trace_id,
        model=model,
        transport=transport,
        prompt_chars=len(prompt),
        prefix_2k_sha256=prefix_small,
        prefix_16k_sha256=prefix_large,
    )


def emit_call_usage(
    *,
    call_id: str,
    call_index: int,
    model: str,
    transport: str,
    usage: dict[str, Any],
    output_chars: int,
) -> None:
    """Emit a ``provider.call_usage`` audit row when the SDK result lands.

    Dumps the whole SDK ``usage`` payload flat under ``usage_<key>`` (and keeps
    the nested original under ``usage_raw``) so every token/cache/cost field the
    SDK returns reaches the trace. Wall duration and TTFT are intentionally NOT
    computed here: the analyzer derives them by joining this row's timestamp to
    the paired ``provider.call_started`` (by ``call_id``) and the first
    ``provider.raw_event`` (by ``call_index``). No-op when audit is disabled.

    Args:
        call_id: Per-call correlation id pairing this row with its started row.
        call_index: Process-local monotonic call index (also on raw_event rows).
        model: The clean model name (SDK ``--model`` value).
        transport: ``"sdk"`` or ``"exec"`` — selects the provider label.
        usage: The raw SDK usage dict (may be empty when the SDK returned none).
        output_chars: Character length of the final response text (a coarse
            output-size proxy independent of the token count).
    """

    if not stream_audit_enabled():
        return
    session_id, turn_id, trace_id = active_gact_ids()
    # Build one field dict rather than passing ``**flat`` alongside explicit
    # kwargs: a usage key of ``keys`` or ``raw`` flattens to ``usage_keys`` /
    # ``usage_raw`` and would collide with the explicit fields, raising a
    # ``TypeError`` that (from _astream_sdk's ``finally``) would mask the real
    # call outcome. ``setdefault`` lets the explicit fields win over the flattened
    # ones, so a malformed usage payload degrades instead of breaking the call.
    fields: dict[str, Any] = {
        "provider": _provider_label(transport),
        "call_id": call_id,
        "call_index": call_index,
        "session_id": session_id,
        "turn_id": turn_id,
        "trace_id": trace_id,
        "model": model,
        "transport": transport,
        "output_chars": output_chars,
        "usage_keys": sorted(str(key) for key in usage),
        "usage_raw": dict(usage),
    }
    for key, value in usage.items():
        fields.setdefault(f"usage_{key}", value)
    stream_audit("provider.call_usage", **fields)


__all__ = [
    "active_gact_ids",
    "emit_call_started",
    "emit_call_usage",
    "prompt_prefix_fingerprint",
]
