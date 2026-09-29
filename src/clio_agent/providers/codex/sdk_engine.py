"""The Codex SDK as a DSPy 3.4 engine (``complete``/``stream`` over ``dspy.lm15.Request``).

The official ``openai_codex`` SDK is a provider only: it runs a read-only, tool-less
thread and answers one prompt string per turn (:mod:`.sdk_client` owns the runtime,
threads and cancellation). This engine carries the typed request across:

* **Text tool calls.** The SDK has no native tools, so the request's tools and every
  message render into the prompt (:mod:`clio_agent.lm.engines.text_tools`) and the
  reply's one ``tool_calls`` block comes back as typed ``ToolCallPart`` s. The engine
  declares this: ``tool_protocol = "text_block"``.
* **One thread per conversation.** Inside an agent loop (an active stateful scope with
  a GACT session and a react scope) the thread for ``(session, scope, model, cwd,
  effort)`` is kept. A call whose messages start with everything that thread already
  holds -- what it was sent plus its own reply -- continues it with only the NEW
  messages; anything else opens a new thread and sends in full with a typed reset
  reason (``first_call`` / ``prefix_mismatch`` / ``session_evicted`` /
  ``provider_error`` / ``provider_compacted`` / ``ops_reset``) on the
  ``provider.stateful`` audit row. Auto-compaction is off on these threads; a
  compaction that happens anyway resets the conversation.
* **Streaming.** Reasoning arrives as thinking deltas, the answer as text deltas held
  back at the tool-call block, then the calls, then usage (cached input included).
* **Errors** raise ``dspy.lm15`` types so DSPy owns retries.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import threading
import uuid
from collections import OrderedDict
from collections.abc import AsyncGenerator, AsyncIterator, Iterator
from dataclasses import dataclass, field
from typing import Any

from dspy.lm15 import (
    AuthError,
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
    UnsupportedModelError,
    Usage,
)
from dspy.lm15 import TimeoutError as LMTimeoutError
from openai_codex.types import ReasoningEffort

from clio_agent.lm.engines.text_tools import (
    StreamSplitter,
    render_messages,
    render_system,
    split_reply,
)
from clio_agent.providers.codex.errors import (
    CodexPlanLimitError,
    CodexSDKError,
    contains_codex_authentication_error,
    is_usage_limit_text,
)
from clio_agent.providers.codex.sdk_audit import emit_call_started, emit_call_usage
from clio_agent.providers.codex.sdk_client import (
    _SDK_CLIENT,
    DEFAULT_TURN_TIMEOUT_S,
    CodexThreadLostError,
    _item_root,
    _normalize_usage,
)
from clio_agent.providers.stateful_common import (
    active_stateful_scope,
    register_scope_registry,
    stateful_reset_payload,
)

__all__ = ["AsyncCodexSDKEngine", "CodexSDKEngine"]


# --------------------------------------------------------------------------- #
# Conversations: what each kept thread already holds                          #
# --------------------------------------------------------------------------- #
@dataclass
class _Conversation:
    thread_id: str
    system: str
    held: tuple[Message, ...]  # the messages the thread was sent


@dataclass
class _Send:
    """One call's plan: the messages to render and whether it continues a thread."""

    key: tuple[str, ...] | None
    messages: tuple[Message, ...]
    thread_id: str | None
    reason: str | None
    call_id: str = field(default_factory=lambda: uuid.uuid4().hex)


class _Conversations:
    """Process-wide registry of kept threads (bounded LRU, thread-safe)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._live: OrderedDict[tuple[str, ...], _Conversation] = OrderedDict()
        self._resets: dict[tuple[str, ...], str] = {}
        self._forward: dict[str, set[tuple[str, ...]]] = {}
        self._to_archive: list[str] = []

    def plan(self, key: tuple[str, ...] | None, request: Request, system: str) -> _Send:
        if key is None:
            return _Send(key=None, messages=request.messages, thread_id=None, reason=None)
        forward = active_stateful_scope()
        with self._lock:
            if forward is not None:
                self._forward.setdefault(forward, set()).add(key)
            pending = self._resets.pop(key, None)
            live = self._live.get(key)
            if live is None:
                return _Send(key, request.messages, None, pending or "first_call")
            new = _new_messages(live, request, system)
            if pending is not None or new is None:
                self._drop(key)
                return _Send(key, request.messages, None, pending or "prefix_mismatch")
            self._live.move_to_end(key)
            return _Send(key, new, live.thread_id, None)

    def opened(self, key: tuple[str, ...], thread_id: str, request: Request, system: str) -> None:
        with self._lock:
            self._drop(key)
            self._live[key] = _Conversation(thread_id, system, request.messages)
            while len(self._live) > _capacity():
                _old, conversation = self._live.popitem(last=False)
                self._to_archive.append(conversation.thread_id)

    def continued(self, key: tuple[str, ...], request: Request) -> None:
        with self._lock:
            live = self._live.get(key)
            if live is not None:
                live.held = request.messages

    def reset(self, key: tuple[str, ...], reason: str) -> None:
        with self._lock:
            self._drop(key)
            self._resets[key] = reason

    def reset_forward(self, forward: str, reason: str) -> None:
        with self._lock:
            keys = set(self._forward.get(forward, set()))
        for key in keys:
            self.reset(key, reason)

    def release_forward(self, forward: str) -> None:
        with self._lock:
            self._forward.pop(forward, None)

    def take_archive(self) -> list[str]:
        with self._lock:
            pending, self._to_archive = self._to_archive, []
            return pending

    def _drop(self, key: tuple[str, ...]) -> None:
        conversation = self._live.pop(key, None)
        if conversation is not None:
            self._to_archive.append(conversation.thread_id)

    def clear_for_tests(self) -> None:
        with self._lock:
            self._live.clear()
            self._resets.clear()
            self._forward.clear()
            self._to_archive.clear()


def _new_messages(live: _Conversation, request: Request, system: str) -> tuple[Message, ...] | None:
    """The messages after what the thread holds and its own reply, else ``None``.

    The thread holds what it was sent (``live.held``) plus the reply it produced; the
    next call must repeat both, then add only tool results / user / developer
    messages. Anything else (an edit, a different system prompt) is not a delta.
    """
    held = live.held
    messages = request.messages
    if system != live.system or len(messages) <= len(held) or messages[: len(held)] != held:
        return None
    if messages[len(held)].role != "assistant":
        return None
    new = messages[len(held) + 1 :]
    if not new or any(m.role == "assistant" for m in new):
        return None
    return new


_CONVERSATIONS = _Conversations()


class _ScopeAdapter:
    """ARC ops reset every conversation a forward drove; threads outlive the forward."""

    def mark_reset(self, scope_token: str, reason: str = "ops_reset") -> None:
        _CONVERSATIONS.reset_forward(scope_token, reason)

    def release(self, scope_token: str) -> None:
        _CONVERSATIONS.release_forward(scope_token)


register_scope_registry(_ScopeAdapter())


def _capacity() -> int:
    """Max kept Codex conversations (``providers.codex.stateful_capacity``, default 128)."""
    from clio_agent import conf  # noqa: PLC0415

    return max(
        1,
        int(
            conf.resolve(
                "providers.codex.stateful_capacity",
                env="CLIO_CODEX_STATEFUL_CAPACITY",
                default=128.0,
                cast=conf.as_float,
            )
        ),
    )


def _conversation_key(model: str, cwd: str, effort: str) -> tuple[str, ...] | None:
    """``(session, run-keyed scope, model, cwd, effort)`` inside an agent loop, else None."""
    from clio_agent.gact import context as _ctx  # noqa: PLC0415

    if active_stateful_scope() is None:
        return None
    session = (_ctx.active_session_id() or "").strip()
    scope = _ctx.run_keyed_scope(_ctx.active_react_scope())
    if not session or not scope:
        return None
    return (session, scope, model, cwd, effort)


# --------------------------------------------------------------------------- #
# The engine                                                                  #
# --------------------------------------------------------------------------- #
class AsyncCodexSDKEngine:
    """Async Codex SDK engine (see the module docstring)."""

    supports_function_calling = True
    supports_reasoning = True
    tool_protocol = "text_block"

    def __init__(
        self,
        model: str,
        *,
        effort: str | None = None,
        cwd: str | None = None,
        timeout: float = DEFAULT_TURN_TIMEOUT_S,
    ) -> None:
        self.model = model
        self.effort = effort or ""
        self.cwd = cwd or tempfile.gettempdir()
        self.timeout = timeout

    async def complete(self, request: Request) -> Response:
        """Run one turn and return the assembled response."""
        return _assemble(request, [event async for event in self.stream(request)])

    async def stream(self, request: Request) -> AsyncGenerator[Any, None]:
        """Run one turn, yielding lm15 stream events."""
        system = render_system(request.system, request.tools)
        key = _conversation_key(self.model, self.cwd, self.effort)
        send = _CONVERSATIONS.plan(key, request, system)
        try:
            async for event in self._turn(request, system, send):
                yield event
        except CodexThreadLostError:
            # Raised before anything was sent or streamed: reset typed, resend in full.
            if send.key is not None:
                _CONVERSATIONS.reset(send.key, "session_evicted")
            send = _CONVERSATIONS.plan(key, request, system)
            async for event in self._turn(request, system, send):
                yield event

    async def aclose(self) -> None:
        """Nothing to release: the SDK client is process-wide."""

    async def _turn(self, request: Request, system: str, send: _Send) -> AsyncIterator[Any]:
        body, images = render_messages(send.messages)
        prompt = body if send.thread_id else f"{system}\n\n{body}" if system else body
        _audit(send, self.model, len(request.messages))
        call_index = _next_call_index()
        emit_call_started(
            call_id=send.call_id, call_index=call_index, model=self.model, prompt=prompt
        )
        turn = _Turn()
        yield StreamStartEvent(model=request.model)
        try:
            async for event in _SDK_CLIENT.stream(
                prompt=prompt,
                images=[_image_url(image) for image in images],
                model=self.model,
                cwd=self.cwd,
                effort=ReasoningEffort(self.effort) if self.effort else None,
                timeout=self.timeout,
                thread_id=send.thread_id,
                keep_thread=send.key is not None,
                on_thread=lambda tid: _CONVERSATIONS.opened(send.key, tid, request, system)
                if send.key is not None
                else None,
                on_compacted=lambda: _CONVERSATIONS.reset(send.key, "provider_compacted")
                if send.key is not None
                else None,
            ):
                for out in turn.handle(event):
                    yield out
        except CodexThreadLostError:
            raise
        except (Exception, asyncio.CancelledError) as exc:
            if send.key is not None:
                _CONVERSATIONS.reset(send.key, "provider_error")
            if isinstance(exc, CodexSDKError):
                raise _typed(exc, self.model) from exc
            raise
        finally:
            _SDK_CLIENT.archive_threads(_CONVERSATIONS.take_archive())
            emit_call_usage(
                call_id=send.call_id,
                call_index=call_index,
                model=self.model,
                usage=turn.usage,
                output_chars=len(turn.reply),
            )
        if send.thread_id and send.key is not None:
            _CONVERSATIONS.continued(send.key, request)
        for out in turn.finish(send.call_id):
            yield out


class CodexSDKEngine(AsyncCodexSDKEngine):
    """Sync twin: drives the async turn on this thread's private event loop."""

    def complete(self, request: Request) -> Response:  # type: ignore[override]
        return _assemble(request, list(self.stream(request)))

    def stream(self, request: Request) -> Iterator[Any]:  # type: ignore[override]
        loop = asyncio.new_event_loop()
        events = AsyncCodexSDKEngine.stream(self, request)
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
        """Nothing to release: the SDK client is process-wide."""


# --------------------------------------------------------------------------- #
# One SDK turn -> lm15 events                                                 #
# --------------------------------------------------------------------------- #
class _Turn:
    """Folds the SDK's turn events into lm15 deltas, usage and the final reply."""

    def __init__(self) -> None:
        self.reply = ""
        self.fallback = ""
        self.usage: dict[str, int] = {}
        self._splitter = StreamSplitter()
        self._summary_parts = 0

    def handle(self, event: Any) -> list[Any]:
        method = str(getattr(event, "method", ""))
        payload = event.payload
        if method == "item/agentMessage/delta":
            text = str(getattr(payload, "delta", "") or "")
            self.reply += text
            shown = self._splitter.push(text)
            return [StreamDeltaEvent(delta=TextDelta(text=shown, part_index=1))] if shown else []
        if method == "item/reasoning/summaryPartAdded":
            self._summary_parts += 1
            if self._summary_parts > 1:
                return [StreamDeltaEvent(delta=ThinkingDelta(text="\n\n", part_index=0))]
            return []
        if method in {"item/reasoning/textDelta", "item/reasoning/summaryTextDelta"}:
            text = str(getattr(payload, "delta", "") or "")
            return [StreamDeltaEvent(delta=ThinkingDelta(text=text, part_index=0))] if text else []
        if method == "thread/tokenUsage/updated":
            self.usage = _normalize_usage(payload) or self.usage
        elif method == "item/completed":
            item = _item_root(payload)
            if str(getattr(item, "type", "")) == "agentMessage":
                phase = getattr(getattr(item, "phase", None), "value", getattr(item, "phase", None))
                if phase == "final_answer" or not self.fallback:
                    self.fallback = str(getattr(item, "text", "") or "")
        return []

    def finish(self, call_id: str) -> list[Any]:
        reply = self.reply or self.fallback
        if not reply.strip():
            raise ServerError("Codex SDK returned empty content (model reply was blank)")
        out: list[Any] = []
        if not self.reply and self.fallback:
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
        out.append(
            StreamEndEvent(
                finish_reason="tool_call" if split.calls else "stop",
                usage=Usage(
                    input_tokens=self.usage.get("input_tokens"),
                    output_tokens=self.usage.get("output_tokens"),
                    cache_read_tokens=self.usage.get("cached_input_tokens"),
                    reasoning_tokens=self.usage.get("reasoning_output_tokens"),
                ),
            )
        )
        return out


def _assemble(request: Request, events: list[Any]) -> Response:
    """Build the ``Response`` a non-streamed call returns from the turn's events."""
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


def _image_url(image: ImagePart) -> str:
    if image.url:
        return image.url
    if image.data:
        return f"data:{image.media_type};base64,{image.data}"
    raise ValueError("a Codex SDK image needs a URL or inline data")


def _typed(exc: CodexSDKError, model: str) -> Exception:
    """Map a Codex SDK failure to the lm15 error DSPy classifies (and retries)."""
    message = str(exc)
    if contains_codex_authentication_error(message):
        return AuthError(message)
    if is_usage_limit_text(message):
        # The plan window is exhausted: terminal until it resets, never retried.
        return CodexPlanLimitError(message)
    if model in message and "not supported when using codex" in message.lower():
        return UnsupportedModelError(message)
    if "reason=codex_sdk_progress_timeout" in message:
        return LMTimeoutError(message)
    return ServerError(message)


def _audit(send: _Send, model: str, total: int) -> None:
    """One ``provider.stateful`` audit row per call inside an agent loop."""
    from clio_agent.runtime.stream_audit import stream_audit, stream_audit_enabled  # noqa: PLC0415

    if send.key is None or not stream_audit_enabled():
        return
    row: dict[str, Any] = {
        "provider": "codex_sdk",
        "transport": "sdk",
        "model": f"codex_sdk/{model}",
        "call_id": send.call_id,
        "stateful_mode": "delta" if send.thread_id else "full",
        "total_messages": total,
        "sent_messages": len(send.messages),
        "conversation": "::".join(send.key[:2]),
        "thread_id": send.thread_id or "",
    }
    if send.reason is not None:
        row.update(stateful_reset_payload(send.reason))
    stream_audit("provider.stateful", **row)
