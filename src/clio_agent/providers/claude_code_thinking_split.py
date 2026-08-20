"""Split Claude Code SDK ``thinking_delta`` text into hidden provider thinking and
the DSPy ChatAdapter contract.

The Claude Code SDK can stream the DSPy contract on the ``thinking_delta`` channel
before it later emits a bursty ``text_delta`` copy. This module owns the boundary
detection that decides where the provider's free-form extended thinking ends and the
structured contract begins, so :mod:`clio_agent.providers.claude_code_litellm` stays a
thin transport and the (subtle) marker logic lives in one testable place (#877/#880).

It also owns the bridge's thinking-channel EMISSION seams: forwarding surviving
provider-thinking text to the live thinking lane (:func:`emit_provider_thinking`),
and the typed no-silent-fallback reason for CoT-REDACTED thinking deltas
(:func:`note_redacted_thinking`) that claude CLI >= 2.1.x produces when the SDK
thinking config omits ``display: "summarized"``. The redaction fact is recorded
at two fidelities: the opt-in ``CLIO_STREAM_AUDIT_LOG`` row (every delta) and a
``provider.thinking.redacted`` durable semantic-trace event (once per call, at
:func:`_emit_redacted_thinking_trace_event`) — without the latter, "zero CoT"
and "CoT sent but fully redacted" were indistinguishable after the fact on
everything durable (trace/API), a silent degradation the cleanup program's
no-silent-fallback rule forbids.
"""

from __future__ import annotations

import re
from typing import Any

from clio_agent.runtime import trace
from clio_agent.runtime.stream_audit import stream_audit

# A DSPy ChatAdapter field header at the START of a line. This mirrors DSPy's own output
# grammar and parser: ``chat_adapter.py`` uses ``field_header_pattern = r"\[\[ ## (\w+) ## \]\]"``
# matched per line via ``field_header_pattern.match(line.strip())`` — a header is only a header
# at a line start. Both anchors are load-bearing:
#   * Line-start (``\A`` or after ``\n``): a mid-line MENTION of a marker — the model narrating
#     its own contract in prose, e.g. ``... it emits `[[ ## next_thought ## ]]` then ...`` — is
#     NOT a contract boundary and stays in provider thinking, so it never latches the contract
#     and never leaks to the render as garbled scaffolding (#877).
#   * ``\w+`` (not a fixed allowlist): a well-formed header for an UNKNOWN field name is still
#     recognized as contract, matching DSPy's grammar, rather than surviving in provider
#     thinking and rendering verbatim once the client marker strip is deleted (#880).
_CONTRACT_HEADER_RE = re.compile(r"(?:\A|\n)[ \t]*\[\[ ## \w+ ## \]\]")

# The literal prefix that opens a header, up to (but not including) the ``\w+`` field name.
# Used to size the straddle hold-back when a header is split across two ``thinking_delta``s.
_HEADER_LITERAL = "[[ ## "


def _partial_header_prefix_len(combined: str) -> int:
    """Length of the trailing suffix of ``combined`` that could still complete into a
    line-start ``[[ ## <field> ## ]]`` header once more text arrives (a straddled marker).

    Only the last (incomplete) line can host a forming header: earlier lines are terminated
    by ``\\n`` and were already scanned for a completed header. Whitespace-only or clearly
    non-header trailing lines return 0 — they are safe to flush now, because a header on the
    next delta still matches at ``\\A``/``\\n``.
    """

    segment = combined[combined.rfind("\n") + 1 :]
    stripped = segment.lstrip(" \t")
    if stripped == "":
        return 0
    if len(stripped) <= len(_HEADER_LITERAL):
        return len(segment) if _HEADER_LITERAL.startswith(stripped) else 0
    if not stripped.startswith(_HEADER_LITERAL):
        return 0
    after = stripped[len(_HEADER_LITERAL) :]
    name = re.match(r"\w+", after)
    if name is None:
        return 0
    return len(segment) if " ## ]]".startswith(after[name.end() :]) else 0


def _split_provider_thinking_contract_delta(
    text: str,
    *,
    marker_tail: str,
    contract_started: bool,
) -> tuple[str, str, str, bool]:
    """Split one SDK ``thinking_delta`` into hidden provider thinking and DSPy contract text.

    Once a ``[[ ## field ## ]]`` header appears **at the start of a line** (DSPy's own header
    grammar — see :data:`_CONTRACT_HEADER_RE`), that suffix is no longer merely provider-internal
    thinking: it is the model's structured contract and must enter the normal LiteLLM text stream
    immediately so field extractors can publish visible deltas over time. A mid-line marker
    mention (the model narrating the format in prose) is left in provider thinking and never
    latches the contract (#877).

    Returns ``(provider_thinking, contract_text, next_tail, next_started)``.
    """

    if not text:
        return "", "", marker_tail, contract_started
    if contract_started:
        return "", text, marker_tail, True

    combined = marker_tail + text
    header = _CONTRACT_HEADER_RE.search(combined)
    if header is not None:
        start = header.start()
        if combined[start : start + 1] == "\n":
            start += 1  # keep the boundary newline with provider thinking
        return combined[:start], combined[start:], "", True

    hold = _partial_header_prefix_len(combined)
    if hold == 0:
        return combined, "", "", False
    split = len(combined) - hold
    return combined[:split], "", combined[split:], False


def emit_provider_thinking(text: str, *, call_index: int, event_index: int) -> None:
    """Forward surviving provider-thinking text to the live thinking lane, audited.

    The single owner of the bridge's "hidden CoT reaches the transcript" emission:
    one ``provider.normalized`` stream-audit row plus the best-effort
    ``note_lm_provider_thinking_delta`` tap that opens the transcript's ``thinking``
    part (``provider_thinking:claude_code_sdk``). No-op for empty text, so callers
    pass the raw split output / marker tail unchecked.
    """

    if not text:
        return
    stream_audit(
        "provider.normalized",
        provider="claude_code_sdk",
        call_index=call_index,
        event_index=event_index,
        source_channel="thinking_delta",
        normalized_event="turn.trace.delta",
        chunk_len=len(text),
        duplicate_suppressed=False,
        head=text[:120],
    )
    try:
        from clio_agent.runtime.lm_activity import (  # noqa: PLC0415
            note_lm_provider_thinking_delta,
        )

        note_lm_provider_thinking_delta(text, provider="claude_code_sdk")
    except Exception:  # noqa: BLE001,S110 - debug stream must not break provider
        pass


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

    This module is a bare LiteLLM transport (imported below ``gact``), so the
    import is lazy and best-effort — a build with no reachable app/session (CLI /
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
