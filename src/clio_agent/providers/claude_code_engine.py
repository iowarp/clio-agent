"""Claude Code as a DSPy 3.4 engine (``complete``/``stream`` over ``dspy.lm15.Request``).

The Claude Agent SDK is a provider only: one pooled client per GACT session
(:mod:`.claude_code_sessions`) answers one query at a time with Claude Code's own tools,
MCP servers, plugins and skills off (:func:`.claude_code_options.build_sdk_options`).
This engine carries the typed request across:

* **Text tool calls.** No native tools, so the request's tools and messages render as
  text (:mod:`clio_agent.lm.engines.text_tools`) and the reply's one ``tool_calls``
  block comes back as typed calls. Images and documents (PDFs) ride as native content
  blocks next to the text.
* **One session per conversation.** Inside an agent loop the SDK session for
  ``(session, scope, model, cwd, thinking)`` is kept
  (:class:`~clio_agent.lm.engines.conversations.ConversationRegistry`): append-only
  calls send only the new messages under the same session id; anything else opens a
  new session and sends in full, with a typed reset reason on the ``provider.stateful``
  audit row.
* **Streaming.** Thinking deltas as thinking, text deltas held back at the tool-call
  block, then the calls, then usage (cache reads and writes included).
* **Errors.** Transport failures and timeouts raise ``dspy.lm15`` types, so DSPy owns
  retries; a refused sign-in, a rejected model and an exhausted plan raise clio's typed
  errors (never retried; the loop re-raises them as themselves).
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
import uuid
from collections.abc import AsyncGenerator, Iterator
from typing import Any

from dspy.lm15 import (
    DocumentPart,
    ImagePart,
    Message,
    Request,
    Response,
    ServerError,
    StreamDeltaEvent,
    StreamEndEvent,
    StreamStartEvent,
    TextDelta,
    TextPart,
    ThinkingDelta,
    ThinkingPart,
    ToolCallDelta,
    ToolCallPart,
    Usage,
)
from dspy.lm15 import TimeoutError as LMTimeoutError

from clio_agent.lm.engines.conversations import ConversationRegistry, Send, conversation_key
from clio_agent.lm.engines.text_tools import (
    TURN_REMINDER,
    StreamSplitter,
    render_messages,
    render_system,
    split_reply,
)
from clio_agent.providers import claude_code_bridge as _bridge
from clio_agent.providers.claude_code_audit import emit_call_started, emit_call_usage
from clio_agent.providers.claude_code_multimodal import native_blocks
from clio_agent.providers.claude_code_options import require_claude_agent_sdk, thinking_key
from clio_agent.providers.claude_code_plan_limit import plan_limit_from_rate_limit_event
from clio_agent.providers.claude_code_result_errors import (
    raise_classified_result_error,
    result_error_detail,
)
from clio_agent.providers.claude_code_sessions import (
    _STREAM_CLIENT_POOL,
    _active_gact_session_id,
    on_client_dropped,
    transient_transport_error_message,
    transient_transport_error_types,
)
from clio_agent.providers.claude_code_stream_events import (
    note_redacted_thinking,
    stream_event_text,
    stream_event_thinking,
)
from clio_agent.providers.stateful_common import stateful_reset_payload

__all__ = ["AsyncClaudeCodeEngine", "ClaudeCodeEngine"]


def _capacity() -> int:
    """Max kept Claude Code sessions (``providers.claude_code.stateful_capacity``)."""
    from clio_agent import conf  # noqa: PLC0415

    return max(
        1,
        int(
            conf.resolve(
                "providers.claude_code.stateful_capacity",
                env="CLIO_CLAUDE_CODE_STATEFUL_CAPACITY",
                default=128.0,
                cast=conf.as_float,
            )
        ),
    )


_CONVERSATIONS = ConversationRegistry(_capacity)
# A dropped pooled client takes every SDK conversation it held with it.
on_client_dropped(lambda session: _CONVERSATIONS.reset_session(session, "session_evicted"))


class AsyncClaudeCodeEngine:
    """Async Claude Code engine (see the module docstring)."""

    supports_function_calling = True
    supports_reasoning = True
    tool_protocol = "text_block"

    def __init__(
        self,
        model: str,
        *,
        thinking: dict[str, Any] | None = None,
        cwd: str | None = None,
        idle_timeout_s: float | None = None,
    ) -> None:
        self.model = model
        self.thinking = thinking
        self.cwd = cwd or os.getcwd()
        self.idle_timeout_s = idle_timeout_s

    def idle_timeout(self) -> float:
        """The longest gap between two streamed messages (#1577).

        The explicit bound when one was given, else ``limits.lm_inter_token_idle_s`` --
        the same window the turn watchdog trusts a streaming call for. The connect has
        its own progress-based bound (:mod:`.claude_code_expiry`).
        """
        if self.idle_timeout_s is not None:
            return self.idle_timeout_s
        from clio_agent.runtime.lm_activity import inter_token_idle_seconds  # noqa: PLC0415

        return inter_token_idle_seconds()

    async def complete(self, request: Request) -> Response:
        """Run one query and return the assembled response."""
        return _assemble(request, [event async for event in self.stream(request)])

    async def stream(self, request: Request) -> AsyncGenerator[Any, None]:
        """Run one query, yielding lm15 stream events."""
        system = render_system(request.system, request.tools)
        key = conversation_key(self.model, self.cwd, thinking_key(self.thinking) or "")
        send = _CONVERSATIONS.plan(key, request, system)
        async for event in self._query(request, system, send):
            yield event

    async def aclose(self) -> None:
        """Nothing to release: the pooled clients belong to their GACT sessions."""

    async def _query(self, request: Request, system: str, send: Send) -> AsyncGenerator[Any, None]:
        # The one SDK import seam: installs once when absent, else a typed
        # ClaudeCodeCLIUnavailableError -- never a raw ImportError mid-call.
        await asyncio.to_thread(require_claude_agent_sdk)
        body, media = render_messages(send.messages, media=(ImagePart, DocumentPart))
        if request.tools:
            body = f"{body}{chr(10) * 2}{TURN_REMINDER}"
        blocks = native_blocks(media)  # typed refusal (size, type, egress) before any send
        session_id = send.handle or uuid.uuid4().hex
        _audit(send, self.model, len(request.messages), session_id)
        call_index = _next_call_index()
        emit_call_started(
            call_id=send.call_id,
            call_index=call_index,
            model=self.model,
            transport="sdk",
            prompt=body,
        )
        gact_session = _active_gact_session_id()
        entry = _STREAM_CLIENT_POOL.entry_for(session_id=gact_session, gact_session_id=gact_session)
        turn = _Turn(self.model, call_index, session_id)
        yield StreamStartEvent(model=request.model)
        try:
            async for message in entry.stream(
                payload=body,
                native_blocks=blocks,
                session_id=session_id,
                idle_timeout=self.idle_timeout(),
                on_construct=_STREAM_CLIENT_POOL.bump_construct,
                model=self.model,
                cwd=self.cwd,
                thinking=self.thinking,
                system_prompt=system,
            ):
                for out in turn.handle(message):
                    yield out
        except TimeoutError as exc:  # typed by claude_code_expiry: connect vs stream idle
            _reset(send)
            raise LMTimeoutError(f"{exc} (model={self.model})") from exc
        except transient_transport_error_types() as exc:
            _reset(send)
            raise ServerError(
                transient_transport_error_message(
                    self.model, exc, call_index=call_index, stderr_tail=entry.stderr_ring.tail()
                )
            ) from exc
        except BaseException:
            _reset(send)
            raise
        finally:
            emit_call_usage(
                call_id=send.call_id,
                call_index=call_index,
                model=self.model,
                transport="sdk",
                usage=turn.usage,
                output_chars=len(turn.reply),
            )
        # Recorded after the query: connecting it may have dropped an older client.
        if send.key is not None:
            if turn.provider_compacted:
                _CONVERSATIONS.reset(send.key, "provider_compacted")
            elif send.handle is None:
                _CONVERSATIONS.opened(send.key, session_id, request, system)
            else:
                _CONVERSATIONS.continued(send.key, request)
        closing = turn.finish(send.call_id)  # records this call's cost share
        # A one-shot session (outside a loop) is never continued; released ones are gone.
        _forget_costs([session_id] if send.key is None else _CONVERSATIONS.take_released())
        for out in closing:
            yield out


class ClaudeCodeEngine(AsyncClaudeCodeEngine):
    """Sync twin: drives the async query on this thread's private event loop."""

    def complete(self, request: Request) -> Response:  # type: ignore[override]
        return _assemble(request, list(self.stream(request)))

    def stream(self, request: Request) -> Iterator[Any]:  # type: ignore[override]
        loop = asyncio.new_event_loop()
        events = AsyncClaudeCodeEngine.stream(self, request)
        try:
            while True:
                try:
                    yield loop.run_until_complete(anext(events))
                except StopAsyncIteration:
                    return
        finally:
            loop.run_until_complete(events.aclose())
            loop.close()

    def close(self) -> None:
        """Nothing to release: the pooled clients belong to their GACT sessions."""


# --------------------------------------------------------------------------- #
# One SDK query -> lm15 events                                                #
# --------------------------------------------------------------------------- #
class _Turn:
    """Folds the SDK's messages into lm15 deltas, usage and the final reply."""

    def __init__(self, model: str, call_index: int = 0, session_id: str = "") -> None:
        self.model = model
        self.call_index = call_index
        self.session_id = session_id
        self.events = 0
        self.redacted_tokens = 0
        self.reply = ""
        self.fallback = ""
        self.usage: dict[str, Any] = {}
        self.stop_reason = ""
        self.assistant_error: str | None = None
        # The CLI compacted its own session (``compact_boundary``): its context no longer
        # matches clio-core's, so the conversation is reset after this call.
        self.provider_compacted = False
        self._splitter = StreamSplitter()

    def handle(self, message: Any) -> list[Any]:
        kind = type(message).__name__
        self.events += 1
        if kind == "StreamEvent":
            out: list[Any] = []
            thinking = stream_event_thinking(message.event)
            if thinking:
                out.append(StreamDeltaEvent(delta=ThinkingDelta(text=thinking, part_index=0)))
            else:
                # A redacted thinking delta has no text to stream: record it, typed.
                self.redacted_tokens = note_redacted_thinking(
                    message.event,
                    call_index=self.call_index,
                    event_index=self.events,
                    total=self.redacted_tokens,
                )
            text = stream_event_text(message.event)
            if text:
                self.reply += text
                shown = self._splitter.push(text)
                if shown:
                    out.append(StreamDeltaEvent(delta=TextDelta(text=shown, part_index=1)))
            return out
        if kind == "AssistantMessage":
            self.assistant_error = getattr(message, "error", None) or self.assistant_error
            texts = [b.text for b in message.content if type(b).__name__ == "TextBlock"]
            if texts:
                self.fallback = "".join(texts)
            self._note_usage(message)
        elif kind == "ResultMessage":
            self._note_usage(message)
            if not self.fallback and getattr(message, "result", None):
                self.fallback = str(message.result)
            if getattr(message, "is_error", False):
                raise_classified_result_error(
                    message, model=self.model, assistant_error=self.assistant_error
                )
                raise ServerError(
                    f"claude agent sdk returned an error for model={self.model}: "
                    f"{result_error_detail(message)}"
                )
        elif kind == "RateLimitEvent":
            plan_limit = plan_limit_from_rate_limit_event(message, model=self.model)
            if plan_limit is not None:
                raise plan_limit
        elif kind == "SystemMessage":
            if getattr(message, "subtype", "") == "compact_boundary":
                self.provider_compacted = True
                _audit_message("provider.compacted", self.model, message)
        else:
            _audit_message("provider.message_unhandled", self.model, message)
        return []

    def _note_usage(self, message: Any) -> None:
        if usage := _bridge.sdk_result_usage(message):
            self.usage = usage
        if getattr(message, "stop_reason", None):
            self.stop_reason = str(message.stop_reason)

    def finish(self, call_id: str) -> list[Any]:
        reply = self.reply or self.fallback
        if not reply.strip():
            raise ServerError(f"claude agent sdk returned empty content (model={self.model})")
        out: list[Any] = []
        if not self.reply:
            shown = self._splitter.push(self.fallback)
            if shown:
                out.append(StreamDeltaEvent(delta=TextDelta(text=shown, part_index=1)))
        split = split_reply(reply, call_prefix=f"call_{call_id[:8]}")
        for index, call in enumerate(split.calls):
            out.append(
                StreamDeltaEvent(
                    delta=ToolCallDelta(
                        input=json.dumps(call.input),
                        part_index=2 + index,
                        id=call.id,
                        name=call.name,
                    )
                )
            )
        finish = "length" if self.stop_reason == "max_tokens" else None
        out.append(
            StreamEndEvent(
                finish_reason=finish or ("tool_call" if split.calls else "stop"),
                usage=Usage(
                    input_tokens=self.usage.get("input_tokens"),
                    output_tokens=self.usage.get("output_tokens"),
                    cache_read_tokens=self.usage.get("cache_read_input_tokens"),
                    cache_write_tokens=self.usage.get("cache_creation_input_tokens"),
                ),
                # The SDK's own subscription-priced cost (absent when it reported none).
                provider_data=_cost_data(self.usage, self.session_id),
            )
        )
        return out


def _assemble(request: Request, events: list[Any]) -> Response:
    """Build the ``Response`` a non-streamed call returns from the query's events."""
    thinking = "".join(
        e.delta.text for e in events if e.type == "delta" and e.delta.type == "thinking"
    )
    text = "".join(e.delta.text for e in events if e.type == "delta" and e.delta.type == "text")
    calls = [
        ToolCallPart(id=e.delta.id, name=e.delta.name, input=json.loads(e.delta.input))
        for e in events
        if e.type == "delta" and e.delta.type == "tool_call"
    ]
    end = next(e for e in events if e.type == "end")
    parts: list[Any] = []
    if thinking:
        parts.append(ThinkingPart(text=thinking))
    if text:
        parts.append(TextPart(text=text))
    parts.extend(calls)
    return Response(
        id=None,
        model=request.model,
        message=Message.assistant(parts or [TextPart(text="")]),
        finish_reason=end.finish_reason or "stop",
        usage=end.usage or Usage(),
        provider_data=end.provider_data,
    )


# --------------------------------------------------------------------------- #
# helpers                                                                     #
# --------------------------------------------------------------------------- #
_CALL_INDEX_LOCK = threading.Lock()
_CALL_INDEX = 0


def _next_call_index() -> int:
    global _CALL_INDEX  # noqa: PLW0603
    with _CALL_INDEX_LOCK:
        _CALL_INDEX += 1
        return _CALL_INDEX


_SESSION_COST_LOCK = threading.Lock()
_SESSION_COST: dict[str, float] = {}  # SDK session id -> cumulative cost reported so far


def _cost_data(usage: dict[str, Any], session_id: str) -> dict[str, Any] | None:
    """This call's own ``{"cost_usd": ...}``, else ``None`` when the SDK reported none.

    The SDK reports ``total_cost_usd`` cumulatively for its session (measured live:
    a kept session's second call reported the first call's cost plus its own), so the
    call's share is the increase since the session's previous report.
    """
    cost = usage.get("cost_usd")
    if cost is None:
        return None
    with _SESSION_COST_LOCK:
        previous = _SESSION_COST.get(session_id, 0.0)
        _SESSION_COST[session_id] = float(cost)
    return {"cost_usd": max(0.0, float(cost) - previous)}


def _forget_costs(session_ids: list[str]) -> None:
    with _SESSION_COST_LOCK:
        for session_id in session_ids:
            _SESSION_COST.pop(session_id, None)


def _audit_message(stage: str, model: str, message: Any) -> None:
    """Record an SDK message the engine does not turn into output (never a silent drop)."""
    from clio_agent.runtime.stream_audit import stream_audit  # noqa: PLC0415

    stream_audit(
        stage,
        provider="claude_code",
        model=model,
        message_type=type(message).__name__,
        subtype=str(getattr(message, "subtype", "") or ""),
    )


def _reset(send: Send) -> None:
    """A failed query leaves the session in an unknown state: resend in full next."""
    if send.key is not None:
        _CONVERSATIONS.reset(send.key, "provider_error")


def _audit(send: Send, model: str, total: int, session_id: str) -> None:
    """One ``provider.stateful`` audit row per call inside an agent loop."""
    from clio_agent.runtime.stream_audit import stream_audit, stream_audit_enabled  # noqa: PLC0415

    if send.key is None or not stream_audit_enabled():
        return
    row: dict[str, Any] = {
        "provider": "claude_code_sdk",
        "transport": "sdk",
        "model": f"claude_code/{model}",
        "call_id": send.call_id,
        "stateful_mode": "delta" if send.handle else "full",
        "total_messages": total,
        "sent_messages": len(send.messages),
        "conversation": "::".join(send.key[:2]),
        "session_id": session_id,
    }
    if send.reason is not None:
        row.update(stateful_reset_payload(send.reason))
    stream_audit("provider.stateful", **row)
