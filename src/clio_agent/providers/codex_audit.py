"""Stream-audit instrumentation for the Codex app-server transport (#896).

Emits the SAME ``provider.call_started`` / ``provider.call_usage`` /
``provider.raw_event`` / ``provider.normalized`` rows the Claude Code provider
does, with a ``codex_app_server`` label, so ``scripts/analyze_turn_waterfall.py``
works unchanged for codex. The shared fingerprint + gact-id helpers are reused
from :mod:`clio_agent.providers.claude_code_audit` (one owner, no duplication);
only the provider label and the codex usage normalization differ.

Codex usage is normalized upstream (:func:`clio_agent.providers.codex_app_server.normalize_usage`)
to the snake-case keys the analyzer joins on (``cache_read_input_tokens`` for
codex's ``cachedInputTokens``, ``reasoning_output_tokens`` for
``reasoningOutputTokens``), so the flattened ``usage_<key>`` fields land in the
exact columns the waterfall reads. Every emitter is gated by the existing
``CLIO_STREAM_AUDIT_LOG`` switch and is free when the audit is off.
"""

from __future__ import annotations

from typing import Any

from clio_agent.providers.claude_code_audit import (
    active_gact_ids,
    prompt_prefix_fingerprint,
)
from clio_agent.runtime.stream_audit import stream_audit, stream_audit_enabled

_PROVIDER = "codex_app_server"


def emit_call_started(*, call_id: str, call_index: int, model: str, prompt: str) -> None:
    """Emit a ``provider.call_started`` row at turn-submission time (codex)."""
    if not stream_audit_enabled():
        return
    session_id, turn_id, trace_id = active_gact_ids()
    prefix_small, prefix_large = prompt_prefix_fingerprint(prompt)
    stream_audit(
        "provider.call_started",
        provider=_PROVIDER,
        call_id=call_id,
        call_index=call_index,
        session_id=session_id,
        turn_id=turn_id,
        trace_id=trace_id,
        model=model,
        transport="app_server",
        prompt_chars=len(prompt),
        prefix_2k_sha256=prefix_small,
        prefix_16k_sha256=prefix_large,
    )


def emit_call_usage(
    *, call_id: str, call_index: int, model: str, usage: dict[str, Any], output_chars: int
) -> None:
    """Emit a ``provider.call_usage`` row when the codex turn's usage lands.

    ``usage`` is the normalized breakdown; every key is flattened to ``usage_<key>``
    (with the raw dict under ``usage_raw``) so the analyzer reads
    ``usage_input_tokens`` / ``usage_output_tokens`` / ``usage_cache_read_input_tokens``
    exactly as it does for Claude.
    """
    if not stream_audit_enabled():
        return
    session_id, turn_id, trace_id = active_gact_ids()
    fields: dict[str, Any] = {
        "provider": _PROVIDER,
        "call_id": call_id,
        "call_index": call_index,
        "session_id": session_id,
        "turn_id": turn_id,
        "trace_id": trace_id,
        "model": model,
        "transport": "app_server",
        "output_chars": output_chars,
        "usage_keys": sorted(str(key) for key in usage),
        "usage_raw": dict(usage),
    }
    for key, value in usage.items():
        fields.setdefault(f"usage_{key}", value)
    stream_audit("provider.call_usage", **fields)


def emit_raw_event(
    *, call_index: int, event_index: int, source_channel: str, text: str, raw_event_type: str
) -> None:
    """Emit a ``provider.raw_event`` row for one codex notification (codex)."""
    if not stream_audit_enabled():
        return
    session_id, turn_id, trace_id = active_gact_ids()
    stream_audit(
        "provider.raw_event",
        provider=_PROVIDER,
        session_id=session_id,
        turn_id=turn_id,
        trace_id=trace_id,
        call_index=call_index,
        event_index=event_index,
        raw_event_type=raw_event_type,
        source_channel=source_channel,
        transport="app_server",
        chunk_len=len(text),
        text_len=len(text),
        head=text[:120],
    )


def emit_normalized(
    *, call_index: int, event_index: int, source_channel: str, normalized_event: str, text: str
) -> None:
    """Emit a ``provider.normalized`` row for one normalized codex chunk (codex)."""
    if not stream_audit_enabled():
        return
    stream_audit(
        "provider.normalized",
        provider=_PROVIDER,
        call_index=call_index,
        event_index=event_index,
        source_channel=source_channel,
        normalized_event=normalized_event,
        chunk_len=len(text),
        duplicate_suppressed=False,
        head=text[:120],
    )


__all__ = ["emit_call_started", "emit_call_usage", "emit_normalized", "emit_raw_event"]
