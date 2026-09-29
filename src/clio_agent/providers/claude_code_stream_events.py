"""Bounded extraction from Claude Agent SDK raw stream events.

Also the typed no-silent-fallback record for CoT-REDACTED thinking deltas
(:func:`note_redacted_thinking`): claude CLI >= 2.1.x streams ``thinking_delta``
events with empty text plus an ``estimated_tokens`` count when the SDK thinking
display is ``omitted``. The fact is recorded at two fidelities -- the opt-in
``CLIO_STREAM_AUDIT_LOG`` row (every delta) and a ``provider.thinking.redacted``
durable semantic-trace event (once per call) -- so "zero CoT" and "CoT sent but
fully redacted" stay distinguishable after the fact.
"""

from __future__ import annotations

from typing import Any

from clio_agent.runtime import trace
from clio_agent.runtime.stream_audit import stream_audit


def stream_event_text(event: dict[str, Any]) -> str:
    """Extract user-visible text from one Claude SDK raw event."""

    event_type = str(event.get("type") or "")
    if event_type == "content_block_delta":
        delta = event.get("delta")
        if isinstance(delta, dict):
            if delta.get("type") == "text_delta":
                return str(delta.get("text") or "")
            if isinstance(delta.get("text"), str):
                return delta["text"]
    if event_type == "content_block_start":
        block = event.get("content_block")
        if isinstance(block, dict) and block.get("type") == "text":
            return str(block.get("text") or "")
    return ""


def stream_event_thinking(event: dict[str, Any]) -> str:
    """Extract provider-internal thinking from one Claude SDK raw event."""

    if str(event.get("type") or "") != "content_block_delta":
        return ""
    delta = event.get("delta")
    if not isinstance(delta, dict) or delta.get("type") != "thinking_delta":
        return ""
    return str(delta.get("thinking") or "")


def _emit_redacted_thinking_trace_event(*, call_index: int, tokens: int) -> None:
    """Best-effort durable trace event for a call's FIRST redacted-thinking delta.

    Mirrors the ``agent.toolset.recorded`` funnel idiom
    (:mod:`clio_agent.gact.agents.toolset_inventory`, commit 4942f779): resolve the
    active GACT app/session from the ambient turn context and emit ONE
    ``provider.thinking.redacted`` semantic event through
    :func:`clio_agent.gact.runtime.globals._emit_semantic_event` — the same funnel
    every other semantic event rides — so the redaction fact reaches the durable
    session trace, not just the opt-in stream-audit JSONL and the log-only
    :func:`clio_agent.runtime.trace.event` WARNING (which together left "provider
    sent zero CoT" and "provider sent CoT but it was fully redacted" with
    IDENTICAL zero-delta signatures on everything durable).

    This module sits below ``gact``, so the import is lazy and best-effort — a build with no reachable app/session (CLI /
    optimizer paths, or ``gact`` not installed in this process) leaves no event,
    but the miss is ALWAYS logged with a structured reason, never a silent pass
    (no-silent-fallback ground rule).
    """
    try:
        from clio_agent.gact.context import (  # noqa: PLC0415
            active_app,
            active_session_id,
            active_trace_id,
            active_turn_id,
        )
        from clio_agent.gact.runtime.globals import _emit_semantic_event  # noqa: PLC0415
    except Exception:  # noqa: BLE001 - gact unavailable (CLI/optimizer paths)
        trace.event(
            "CLAUDE-CODE-THINKING",
            "provider.thinking.redacted skipped call=%d reason=gact_unavailable",
            call_index,
        )
        return
    app = active_app()
    sid = active_session_id()
    if app is None or not sid:
        trace.event(
            "CLAUDE-CODE-THINKING",
            "provider.thinking.redacted skipped call=%d reason=no_app_or_session",
            call_index,
        )
        return
    try:
        _emit_semantic_event(
            app,
            sid,
            "provider.thinking.redacted",
            turn_id=active_turn_id(),
            trace_id=active_trace_id(),
            status="completed",
            summary=f"Provider redacted chain-of-thought (call {call_index}).",
            provider={"provider_id": "claude_code_sdk"},
            payload={
                "call_index": call_index,
                "session_id": sid,
                "provider": "claude_code_sdk",
                "thinking_tokens_estimated": tokens,
                "reason": "provider_thinking_redacted",
            },
        )
    except Exception as exc:  # noqa: BLE001 - capture must never break the call
        trace.event(
            "CLAUDE-CODE-THINKING",
            "provider.thinking.redacted emit failed call=%d: %r",
            call_index,
            exc,
        )


def note_redacted_thinking(
    event: dict[str, Any], *, call_index: int, event_index: int, total: int
) -> int:
    """Typed no-silent-fallback reason for a CoT-REDACTED thinking delta.

    claude CLI >= 2.1.x defaults the SDK thinking display to ``omitted``
    (signature-only): ``thinking_delta`` events arrive with ``thinking: ""`` plus an
    ``estimated_tokens`` count, and the AssistantMessage ThinkingBlock carries only
    a signature (verified live 2026-08-05 on CLI 2.1.222 / claude-agent-sdk
    0.2.128). There is no text to stream, so nothing can honestly reach a
    ``thinking`` part (the streamed-part path drops empty deltas by design, and
    authoring placeholder text into a model-output lane is forbidden). Instead of
    the delta vanishing silently, every redacted delta records a structured
    ``provider_thinking_redacted`` reason carrying the token estimate (stream
    audit), and the FIRST one per call emits a ``trace.event`` naming the fix
    (``display: "summarized"`` in the SDK thinking config).

    Returns the running redacted-token total for this call; unchanged for any
    event that is not a redacted thinking delta.
    """

    if str(event.get("type") or "") != "content_block_delta":
        return total
    delta = event.get("delta")
    if not isinstance(delta, dict) or delta.get("type") != "thinking_delta":
        return total
    if str(delta.get("thinking") or ""):
        return total  # real CoT text present — the normal thinking lane owns it
    try:
        tokens = max(0, int(delta.get("estimated_tokens") or 0))
    except (TypeError, ValueError):
        tokens = 0
    if tokens == 0:
        return total
    stream_audit(
        "provider.normalized",
        provider="claude_code_sdk",
        call_index=call_index,
        event_index=event_index,
        source_channel="thinking_delta",
        normalized_event="turn.trace.delta",
        chunk_len=0,
        duplicate_suppressed=True,
        duplicate_reason="provider_thinking_redacted",
        thinking_tokens_estimated=tokens,
    )
    if total == 0:
        trace.event(
            "CLAUDE-CODE-THINKING",
            "provider_thinking_redacted call=%d est_tokens=%d — the CLI omitted the "
            "CoT text (thinking display 'omitted'); send display='summarized' in "
            "the SDK thinking config to receive it",
            call_index,
            tokens,
        )
        _emit_redacted_thinking_trace_event(call_index=call_index, tokens=tokens)
    return total + tokens


__all__ = ["note_redacted_thinking", "stream_event_text", "stream_event_thinking"]
