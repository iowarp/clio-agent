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
from collections.abc import AsyncGenerator, AsyncIterator, Iterator
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

from clio_agent.lm.engines.conversations import ConversationRegistry, Send, conversation_key
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
from clio_agent.providers.native_attachment_bounds import (
    base64_byte_length,
    check_block_bytes,
    check_total_bytes,
)
from clio_agent.providers.stateful_common import stateful_reset_payload

__all__ = ["AsyncCodexSDKEngine", "CodexSDKEngine"]


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


_CONVERSATIONS = ConversationRegistry(_capacity)


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
        key = conversation_key(self.model, self.cwd, self.effort)
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

    async def _turn(self, request: Request, system: str, send: Send) -> AsyncIterator[Any]:
        body, images = render_messages(send.messages)
        inputs = _image_inputs(images)  # typed refusal of oversized images before any send
        prompt = body if send.handle else f"{system}\n\n{body}" if system else body
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
                images=inputs,
                model=self.model,
                cwd=self.cwd,
                effort=ReasoningEffort(self.effort) if self.effort else None,
                timeout=self.timeout,
                thread_id=send.handle,
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
            _SDK_CLIENT.archive_threads(_CONVERSATIONS.take_released())
            emit_call_usage(
                call_id=send.call_id,
                call_index=call_index,
                model=self.model,
                usage=turn.usage,
                output_chars=len(turn.reply),
            )
        if send.handle and send.key is not None:
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


def _image_inputs(images: list[ImagePart]) -> list[str]:
    """The SDK image inputs, each and all within the native attachment ceilings.

    Raises:
        NativeAttachmentTooLargeError: One image, or all of them together, too large.
        ValueError: An image with neither inline data nor a URL.
    """
    inline = 0
    urls: list[str] = []
    for image in images:
        if image.data:
            size = base64_byte_length(image.data)
            check_block_bytes("image", size, label=image.media_type)
            inline += size
            urls.append(f"data:{image.media_type};base64,{image.data}")
        elif image.url:
            urls.append(image.url)
        else:
            raise ValueError("a Codex SDK image needs a URL or inline data")
    check_total_bytes(inline)
    return urls


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


def _audit(send: Send, model: str, total: int) -> None:
    """One ``provider.stateful`` audit row per call inside an agent loop."""
    from clio_agent.runtime.stream_audit import stream_audit, stream_audit_enabled  # noqa: PLC0415

    if send.key is None or not stream_audit_enabled():
        return
    row: dict[str, Any] = {
        "provider": "codex_sdk",
        "transport": "sdk",
        "model": f"codex_sdk/{model}",
        "call_id": send.call_id,
        "stateful_mode": "delta" if send.handle else "full",
        "total_messages": total,
        "sent_messages": len(send.messages),
        "conversation": "::".join(send.key[:2]),
        "thread_id": send.handle or "",
    }
    if send.reason is not None:
        row.update(stateful_reset_payload(send.reason))
    stream_audit("provider.stateful", **row)
