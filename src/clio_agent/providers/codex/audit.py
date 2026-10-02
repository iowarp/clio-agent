"""Stream-audit instrumentation for the Codex direct transport's WebSocket calls.

Emits the SAME ``provider.call_started`` / ``provider.call_usage`` /
``provider.raw_event`` rows the Claude Code provider does, labelled
``codex_direct`` / ``websocket``, so ``scripts/analyze_turn_waterfall.py`` works
unchanged for Codex. The shared fingerprint + gact-id helpers are reused from
:mod:`clio_agent.providers.claude_code_audit` (one owner, no duplication); only
the provider label and the usage normalization differ.

Every emitter is gated by the existing ``CLIO_STREAM_AUDIT_LOG`` switch and is
free when the audit is off.
"""

from __future__ import annotations

from typing import Any

from clio_agent.providers.claude_code_audit import (
    active_gact_ids,
    prompt_prefix_fingerprint,
)
from clio_agent.runtime.stream_audit import stream_audit, stream_audit_enabled

_PROVIDER = "codex_direct"
_TRANSPORT = "websocket"


def emit_call_started(*, call_id: str, call_index: int, model: str, prompt: str) -> None:
    """Emit a ``provider.call_started`` row when a ``response.create`` frame is sent."""
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
        transport=_TRANSPORT,
        prompt_chars=len(prompt),
        prefix_2k_sha256=prefix_small,
        prefix_16k_sha256=prefix_large,
    )


def emit_call_usage(
    *, call_id: str, call_index: int, model: str, usage: dict[str, Any], output_chars: int
) -> None:
    """Emit a ``provider.call_usage`` row when the reply's usage lands.

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
        "transport": _TRANSPORT,
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
    """Emit a ``provider.raw_event`` row for one streamed backend event."""
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
        transport=_TRANSPORT,
        chunk_len=len(text),
        text_len=len(text),
        head=text[:120],
    )


__all__ = ["emit_call_started", "emit_call_usage", "emit_raw_event"]
