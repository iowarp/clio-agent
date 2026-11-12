"""Claude Code as a DSPy 3.4 engine (``complete``/``stream`` over ``dspy.lm15.Request``).

The Claude Agent SDK is a provider only: one pooled client per GACT session
(:mod:`.claude_code_sessions`) answers one query at a time with Claude Code's own tools,
MCP servers, plugins and skills off (:func:`.claude_code_options.build_sdk_options`).
This engine carries the typed request across:

* **Text tool calls.** No native tools, so the request's tools and messages render as
  text (:mod:`clio_agent.lm.engines.text_tools`) and the reply's one ``tool_calls``
  block comes back as typed calls. Images and documents (PDFs) ride as native content
  blocks next to the text.
* **One conversation per client.** Inside an agent loop the pooled client's
  conversation is kept (:mod:`.claude_code_conversation`): an append-only call sends
  only the new messages; anything else clears the CLI and sends in full, with a typed
  reset reason on the ``provider.stateful`` audit row. Calls outside a loop run on the
  session's side client.
* **Streaming.** Thinking deltas as thinking, text deltas held back at the tool-call
  block, then the calls, then usage (cache reads and writes included).
* **Errors.** Transport failures and timeouts raise ``dspy.lm15`` types, so DSPy owns
  retries; a refused sign-in, a rejected model and an exhausted plan raise clio's typed
  errors (never retried; the loop re-raises them as themselves).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
import uuid
from collections.abc import AsyncGenerator, Iterator
from contextlib import aclosing
from typing import Any

from dspy.lm15 import (
    DocumentPart,
    FunctionTool,
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

from clio_agent.lm.engines.conversations import conversation_key
from clio_agent.lm.engines.invoke_calls import text_tool_protocol, writing_past_block
from clio_agent.lm.engines.text_tools import (
    FENCE,
    INVALID_TOOL_CALL,
    ReplySplit,
    StreamSplitter,
    ToolSchemas,
    callable_tools,
    render_messages,
    render_system,
    split_reply,
    turn_reminder,
)
from clio_agent.providers import claude_code_bridge as _bridge
from clio_agent.providers.claude_code_audit import emit_call_started, emit_call_usage
from clio_agent.providers.claude_code_clear import side_key
from clio_agent.providers.claude_code_conversation import Send, plan, record_sent
from clio_agent.providers.claude_code_conversation import reset as reset_conversation
from clio_agent.providers.claude_code_cwd import session_cwd
from clio_agent.providers.claude_code_expiry import (
    ClaudeCodeIdleTimeout,
    StreamSilence,
    stall_first_wait_s,
    stall_waits,
)
from clio_agent.providers.claude_code_lifecycle import DEAD_ENTRY_MARKER
from clio_agent.providers.claude_code_multimodal import native_blocks
from clio_agent.providers.claude_code_options import require_claude_agent_sdk, thinking_key
from clio_agent.providers.claude_code_output_progress import PROTOCOL_WORD as _PROTOCOL_WORD
from clio_agent.providers.claude_code_output_progress import FormattingProgress
from clio_agent.providers.claude_code_plan_limit import plan_limit_from_rate_limit_event
from clio_agent.providers.claude_code_result_errors import (
    raise_classified_result_error,
    result_error_detail,
)
from clio_agent.providers.claude_code_sessions import (
    _STREAM_CLIENT_POOL,
    _active_gact_session_id,
    transient_transport_error_message,
    transient_transport_error_types,
)
from clio_agent.providers.claude_code_stream_events import (
    note_redacted_thinking,
    stream_event_text,
    stream_event_thinking,
)
from clio_agent.providers.stateful_common import stateful_reset_payload
from clio_agent.providers.usage.capture import record_rate_limit_usage

__all__ = ["AsyncClaudeCodeEngine", "ClaudeCodeEngine"]


logger = logging.getLogger(__name__)

#: How often (in characters) a reply with a call block is checked for text past it.
_PAST_CALLS_STRIDE = 128
#: A reply's opening lines that are each blank or one protocol word.
_PROTOCOL_LINES = re.compile(r"(?:[ \t]*(?:_[a-z][a-z0-9_]*)?[ \t]*\r?\n)+")


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
        self.cwd = cwd  # None: the session's workspace (claude_code_cwd)
        self.idle_timeout_s = idle_timeout_s

    def idle_timeout(self) -> float:
        """The first stall wait: silence this long starts a visible retry (#1577).

        The explicit bound when one was given, else ``limits.lm_stall_wait_s`` (30 s).
        Each later stall waits longer (:func:`.claude_code_expiry.stall_waits`); none
        fails the turn. The connect has its own progress-based bound.
        """
        if self.idle_timeout_s is not None:
            return self.idle_timeout_s
        return stall_first_wait_s()

    async def complete(self, request: Request) -> Response:
        """Run one query and return the assembled response."""
        return _assemble(request, [event async for event in self.stream(request)])

    async def stream(self, request: Request) -> AsyncGenerator[Any, None]:
        """Run one query, yielding lm15 stream events."""
        system = render_system(request.system, request.tools)
        cwd = self.cwd or session_cwd(_active_gact_session_id())
        identity = (self.model, cwd, thinking_key(self.thinking) or "")
        kept = conversation_key(*identity) is not None
        async for event in self._query(request, system, cwd, identity, kept):
            yield event

    async def aclose(self) -> None:
        """Nothing to release: the pooled clients belong to their GACT sessions."""

    async def _query(
        self, request: Request, system: str, cwd: str, identity: tuple[str, ...], kept: bool
    ) -> AsyncGenerator[Any, None]:
        # The one SDK import seam: installs once when absent, else a typed
        # ClaudeCodeCLIUnavailableError -- never a raw ImportError mid-call.
        await asyncio.to_thread(require_claude_agent_sdk)
        gact_session = _active_gact_session_id()
        # One-shot calls run on the session's side client: never on an agent's conversation.
        pool_key = gact_session if kept else side_key(gact_session)
        entry = _STREAM_CLIENT_POOL.entry_for(session_id=pool_key, gact_session_id=pool_key)
        send = plan(entry, request, system, identity, kept=kept)
        body, blocks = _payload(request, send)  # typed refusal before any send
        session_id = send.handle or uuid.uuid4().hex
        _audit(send, self.model, len(request.messages), session_id, pool_key)
        call_index = _next_call_index()
        emit_call_started(
            call_id=send.call_id,
            call_index=call_index,
            model=self.model,
            transport="sdk",
            prompt=body,
        )
        available_tools = callable_tools(
            [tool.name for tool in request.tools if isinstance(tool, FunctionTool)]
        )
        schemas = ToolSchemas(
            {tool.name: tool.parameters for tool in request.tools if isinstance(tool, FunctionTool)}
        )

        def new_turn(sid: str) -> _Turn:
            return _Turn(
                self.model, call_index, sid, available_tools=available_tools, schemas=schemas
            )

        turn = new_turn(session_id)
        yield StreamStartEvent(model=request.model)
        received = False
        dead_entry_retried = False
        stall = _StallLadder(self.idle_timeout())
        try:
            while True:
                try:
                    async with aclosing(
                        entry.stream(
                            payload=body,
                            native_blocks=blocks,
                            session_id=session_id,
                            idle_timeout=stall.wait_s,
                            on_construct=_STREAM_CLIENT_POOL.bump_construct,
                            model=self.model,
                            cwd=cwd,
                            thinking=self.thinking,
                            system_prompt=system,
                            fresh=send.handle is None,
                            on_silence=stall.on_silence,
                        )
                    ) as messages:
                        async for message in messages:
                            if isinstance(message, StreamSilence):
                                stall.still_waiting(message)
                                continue
                            received = True
                            for out in turn.handle(message):
                                stall.observe(out)
                                yield out
                            if turn.protocol_error or turn.past_calls:
                                break
                    break
                except ClaudeCodeIdleTimeout as exc:
                    # Nothing of the reply is on screen yet: resend on a fresh client
                    # and wait longer. Never a fixed clock that fails the turn; the
                    # person sees each retry and can stop it.
                    if stall.shown or _cancel_requested():
                        raise
                    _reset(entry, send)
                    stall.resend(exc)
                    entry = _STREAM_CLIENT_POOL.entry_for(
                        session_id=pool_key, gact_session_id=pool_key
                    )
                    send = plan(entry, request, system, identity, kept=kept)
                    body, blocks = _payload(request, send)
                    session_id = send.handle or uuid.uuid4().hex
                    turn = new_turn(session_id)
                    received = False
                except RuntimeError as exc:
                    # The session's pooled entry was released (an abandoned stream, a
                    # turn-end release, the idle sweep) after this call took it and
                    # before anything was sent. Its typed error is retryable: take the
                    # session's fresh entry once and plan again against it (it holds
                    # no conversation, so the send is full). On the record, never silent.
                    if received or dead_entry_retried or DEAD_ENTRY_MARKER not in str(exc):
                        raise
                    dead_entry_retried = True
                    logger.info(
                        "claude_code query reconnected reason=dead_entry_reconnect "
                        "model=%s session=%s",
                        self.model,
                        gact_session,
                    )
                    entry = _STREAM_CLIENT_POOL.entry_for(
                        session_id=pool_key, gact_session_id=pool_key
                    )
                    send = plan(entry, request, system, identity, kept=kept)
                    body, blocks = _payload(request, send)
                    session_id = send.handle or session_id
        except TimeoutError as exc:  # typed by claude_code_expiry: connect vs stream idle
            _reset(entry, send)
            raise LMTimeoutError(f"{exc} (model={self.model})") from exc
        except transient_transport_error_types() as exc:
            _reset(entry, send)
            raise ServerError(
                transient_transport_error_message(
                    self.model, exc, call_index=call_index, stderr_tail=entry.stderr_ring.tail()
                )
            ) from exc
        except BaseException:
            _reset(entry, send)
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
        closing = turn.finish(send.call_id)  # records this call's cost share
        if not send.kept:
            _forget_costs([session_id])  # a one-shot conversation is never continued
        elif turn.protocol_error:
            _reset(entry, send)
        elif turn.provider_compacted:
            reset_conversation(entry, "provider_compacted")
        elif turn.overran:
            reset_conversation(entry, "reply_past_calls")
        elif replaced := record_sent(entry, send, session_id, request, system):
            _forget_costs([replaced])  # the conversation it replaced is over
        for out in closing:
            yield out


def _payload(request: Request, send: Send) -> tuple[str, Any]:
    """The text body and native media blocks of ``send``'s messages."""
    body, media = render_messages(send.messages, media=(ImagePart, DocumentPart))
    if request.tools:
        body = f"{body}{chr(10) * 2}{turn_reminder()}"
    return body, native_blocks(media)


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

    #: Names the transport in this turn's own errors.
    label = "claude agent sdk"
    #: Anthropic usage: the SDK's ``input_tokens`` leaves out the cache read and written.
    input_excludes_cache = True

    def __init__(
        self,
        model: str,
        call_index: int = 0,
        session_id: str = "",
        *,
        available_tools: frozenset[str] = frozenset(),
        schemas: dict[str, Any] | None = None,
    ) -> None:
        self.model = model
        self.call_index = call_index
        self.session_id = session_id
        self.available_tools = available_tools
        self.schemas = schemas or {}
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
        # The model wrote on past its call block; the CLI's history holds the invented rest.
        self.overran = False
        invoke = text_tool_protocol() == "invoke"
        self._splitter = StreamSplitter(available_tools if invoke else ())
        # The reply went on past its first call block: the stream stops there, since
        # nothing past the block is ever the step's (``reply_past_calls``). Checked every
        # :data:`_PAST_CALLS_STRIDE` characters once a call block has begun.
        self.past_calls = False
        self._past_check_at = 0
        self.protocol_error: str | None = None
        self._progress = FormattingProgress(enabled=bool(available_tools))
        self._held = ""  # streamed text held back while it could still be non-progress
        self.dropped_word = ""  # one word written before the fence, not shown
        self._released = False  # whether any of this reply's text was released yet

    def handle(self, message: Any) -> list[Any]:
        if self.protocol_error:
            return []
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
                self._on_text(text, out)
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
            record_rate_limit_usage(message)
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

    def _on_text(self, text: str, out: list[Any]) -> None:
        """Fold one streamed text delta: held back while undecided, split at the fence."""
        cutoff = self._progress.cutoff(text)
        if cutoff is not None:
            self.protocol_error = "formatting_only_repetition"
            self.reply = (self.reply + text[:cutoff])[:4096]
            self._held = ""  # never shown
            return
        self.reply += text
        if (
            self._splitter.stopped
            and self.available_tools
            and len(self.reply) >= self._past_check_at
        ):
            self._past_check_at = len(self.reply) + _PAST_CALLS_STRIDE
            if writing_past_block(self.reply, self.available_tools):
                self.past_calls = True
                logger.info(
                    "provider reply stopped past call block reason=reply_past_calls "
                    "model=%s chars=%d",
                    self.model,
                    len(self.reply),
                )
        if self._progress.undecided:
            self._held += text
            return
        release, self._held = self._without_word_preamble(self._held, text), ""
        shown = self._splitter.push(release)
        if shown:
            out.append(StreamDeltaEvent(delta=TextDelta(text=shown, part_index=1)))

    def _without_word_preamble(self, held: str, text: str) -> str:
        """Text released to the splitter. In the reply's first release, opening lines of
        protocol words (``_projectid_placeholder`` a few times, then prose) and one such word
        right before the fence are not a preamble and are not shown."""
        release, first, self._released = held + text, not self._released, True
        if not first:
            return release
        if (lines := _PROTOCOL_LINES.match(release)) and "_" in lines.group():
            self.dropped_word, release = lines.group().strip(), release[lines.end() :]
        fence_at = release.find(FENCE)
        before = release[:fence_at].strip() if fence_at >= 0 else ""
        if _PROTOCOL_WORD.fullmatch(before):
            self.dropped_word = f"{self.dropped_word}\n{before}".strip()
            release = release[fence_at:]
        return release

    def _note_usage(self, message: Any) -> None:
        if usage := _bridge.sdk_result_usage(message):
            self.usage = usage
        if getattr(message, "stop_reason", None):
            self.stop_reason = str(message.stop_reason)

    def _end_usage(self) -> Usage:
        """The call's usage in lm15's convention for a provider not named ``anthropic``.

        lm15 and DSPy read ``input_tokens`` as the whole prompt, with the cache counts as
        parts of it (``prompt_tokens``, the context reading, the cost split). Anthropic
        reports the uncached rest only, so the cache read and written are added here.
        """
        cache_read = self.usage.get("cache_read_input_tokens")
        cache_write = self.usage.get("cache_creation_input_tokens")
        input_tokens = self.usage.get("input_tokens")
        if input_tokens is not None and self.input_excludes_cache:
            input_tokens = int(input_tokens) + int(cache_read or 0) + int(cache_write or 0)
        return Usage(
            input_tokens=input_tokens,
            output_tokens=self.usage.get("output_tokens"),
            cache_read_tokens=cache_read,
            cache_write_tokens=cache_write,
        )

    def finish(self, call_id: str) -> list[Any]:
        reply = self.reply or self.fallback
        # Some SDK routes return only an assembled AssistantMessage/ResultMessage.
        if not self.reply and self._progress.cutoff(reply) is not None:
            self.protocol_error = "formatting_only_repetition"
            reply = reply[:4096]
            self.fallback = reply
        if not self.protocol_error and self._progress.only_repeats():
            # A whole reply of one word repeated (``_call_placeholder`` twice) is not an
            # answer either; the model gets the same feedback instead of ending the turn.
            self.protocol_error = "formatting_only_repetition"
            reply = reply[:4096]
        if not reply.strip():
            raise ServerError(f"{self.label} returned empty content (model={self.model})")
        out: list[Any] = []
        if not self.reply and not self.protocol_error:
            shown = self._splitter.push(self.fallback)
            if shown:
                out.append(StreamDeltaEvent(delta=TextDelta(text=shown, part_index=1)))
        held, self._held = ("" if self.protocol_error else self._held), ""
        split = (
            ReplySplit(
                text="",
                calls=[
                    ToolCallPart(
                        id=f"call_{call_id[:8]}_0",
                        name=INVALID_TOOL_CALL,
                        input={
                            "reason": self.protocol_error,
                            "error": "The response repeated only line breaks or one word without an action. "
                            "Continue with meaningful text or one complete tool_calls block. "
                            "No operation was submitted by this response; accepted tasks still run.",
                        },
                    )
                ],
            )
            if self.protocol_error
            else split_reply(
                reply,
                call_prefix=f"call_{call_id[:8]}",
                available_tools=self.available_tools,
                schemas=self.schemas,
            )
        )
        if self.dropped_word:
            # Opening protocol words were not a preamble and were never shown. On the record.
            logger.info(
                "provider reply text dropped word reason=%s model=%s call_id=%s chars=%d "
                "fenced_calls=%d",
                "word_only_beside_calls" if split.calls else "protocol_words_before_text",
                self.model,
                call_id,
                len(self.dropped_word),
                len(split.calls),
            )
        if held and split.calls and _PROTOCOL_WORD.fullmatch(held.strip()):
            # A step's only text was one protocol word beside its calls (such as
            # ``_tool_call_placeholder``): not a preamble, so it is not shown. On the record.
            logger.info(
                "provider reply text dropped word reason=word_only_beside_calls "
                "model=%s call_id=%s chars=%d fenced_calls=%d",
                self.model,
                call_id,
                len(held),
                len(split.calls),
            )
        elif held and (shown := self._splitter.push(held)):
            out.append(StreamDeltaEvent(delta=TextDelta(text=shown, part_index=1)))  # an answer
        if words := self._splitter.dropped_fence_words:
            logger.info(
                "provider reply text dropped fence words reason=fence_word_in_text "
                "model=%s call_id=%s lines=%d fenced_calls=%d",
                self.model,
                call_id,
                words,
                len(split.calls),
            )
        if dropped := self._splitter.dropped_invoke_markup:
            # The model also wrote its calls as invoke markup in its prose; only the fenced
            # block runs, so the markup was not shown. On the record, never silent.
            logger.info(
                "provider reply text dropped invoke markup reason=invoke_markup_in_text "
                "model=%s call_id=%s fragments=%d fenced_calls=%d",
                self.model,
                call_id,
                dropped,
                len(split.calls),
            )
        if split.overrun:
            # Written past the call block, before any result existed (invented results,
            # prose about them, more calls): never run or shown. On the record.
            self.overran = True
            logger.info(
                "provider reply text dropped past call block reason=reply_past_calls "
                "model=%s call_id=%s chars=%d invented_results=%s later_calls=%d calls=%d",
                self.model,
                call_id,
                len(split.overrun),
                "[tool results]" in split.overrun or "<function_results>" in split.overrun,
                split.overrun.count("<invoke "),
                len(split.calls),
            )
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
                usage=self._end_usage(),
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


def _cancel_requested() -> bool:
    from clio_agent.runtime.cancellation import cancellation_requested  # noqa: PLC0415

    return cancellation_requested()


class _StallLadder:
    """One call's stall handling: growing waits, a visible notice at each, no deadline.

    Before any of the reply is on screen a stall resends the query; once text is shown
    a resend would repeat it, so the same stream is waited on, longer each time. The
    person sees every stall and can stop the turn; nothing here fails it.
    """

    def __init__(self, first_wait_s: float) -> None:
        self._waits = stall_waits(first_wait_s)
        self._lock = threading.Lock()
        self.wait_s = next(self._waits)
        self.attempt = 1
        self.shown = False  # read on the client's owner-loop thread

    def observe(self, event: Any) -> None:
        delta = getattr(event, "delta", None)
        if isinstance(delta, ToolCallDelta) or (isinstance(delta, TextDelta) and delta.text):
            self.shown = True

    def _advance(self) -> float:
        with self._lock:
            self.wait_s = next(self._waits)
            return self.wait_s

    def on_silence(self, silent_s: float) -> float | None:
        """The relay's stall decision (owner-loop thread): wait longer, or resend."""
        del silent_s
        return self._advance() if self.shown else None

    def still_waiting(self, silence: StreamSilence) -> None:
        _stall_notice(
            "Claude Code has sent nothing for "
            f"{_duration(silence.silent_s)}. Still waiting for the rest of the reply; next "
            f"check in {_duration(silence.next_wait_s)}. Stop the turn to cancel.",
            silent_s=silence.silent_s,
            next_wait_s=silence.next_wait_s,
            action="wait",
        )

    def resend(self, exc: ClaudeCodeIdleTimeout) -> None:
        self.attempt += 1
        wait = self._advance()
        _stall_notice(
            f"Claude Code sent nothing for {_duration(exc.idle_s)}. Retrying the request "
            f"(attempt {self.attempt}) and waiting up to {_duration(wait)}. "
            "Stop the turn to cancel.",
            silent_s=exc.idle_s,
            next_wait_s=wait,
            retry_attempt=self.attempt,
            action="resend",
        )


def _duration(seconds: float) -> str:
    whole = round(seconds)
    if whole < 60:
        return f"{whole} s"
    minutes, rest = divmod(whole, 60)
    return f"{minutes} min" if not rest else f"{minutes} min {rest} s"


def _stall_notice(text: str, **metadata: Any) -> None:
    """Show the stall to the person (and on the record), never to the model."""
    from clio_agent.runtime.stream_audit import stream_audit  # noqa: PLC0415

    logger.warning("claude_code stream stall %s", " ".join(f"{k}={v}" for k, v in metadata.items()))
    stream_audit("lm.stream.stall", reason="model_stream_stalled", **metadata)
    try:
        from clio_agent.gact.model_notices import post_model_notice  # noqa: PLC0415

        post_model_notice("model_stream_stall", text, **metadata)
    except Exception:  # noqa: BLE001 - showing the notice must never break the call
        logger.warning("claude_code stall notice failed", exc_info=True)


def _reset(entry: Any, send: Send) -> None:
    """A failed query leaves the conversation in an unknown state: resend in full next."""
    if send.kept:
        reset_conversation(entry, "provider_error")


def _audit(send: Send, model: str, total: int, session_id: str, conversation: str) -> None:
    """One ``provider.stateful`` audit row per call inside an agent loop."""
    from clio_agent.runtime.stream_audit import stream_audit, stream_audit_enabled  # noqa: PLC0415

    if not send.kept or not stream_audit_enabled():
        return
    row: dict[str, Any] = {
        "provider": "claude_code_sdk",
        "transport": "sdk",
        "model": f"claude_code/{model}",
        "call_id": send.call_id,
        "stateful_mode": "delta" if send.handle else "full",
        "total_messages": total,
        "sent_messages": len(send.messages),
        "conversation": conversation,
        "session_id": session_id,
    }
    if send.reason is not None:
        row.update(stateful_reset_payload(send.reason))
    stream_audit("provider.stateful", **row)
