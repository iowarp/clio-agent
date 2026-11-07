"""GitHub Copilot as a DSPy 3.4 engine (``complete``/``stream`` over ``dspy.lm15.Request``).

The Copilot SDK is a model transport only: one pooled runtime per GACT session
(:mod:`.sessions`) answers one send at a time with every Copilot tool, MCP server,
skill, instruction file and hook off (:mod:`.options`). This engine carries the typed
request across the same way the Claude Code engine does:

* **Text tool calls.** No native tools: the request's tools and messages render as text
  (:mod:`clio_agent.lm.engines.text_tools`) and the reply's one ``tool_calls`` block
  comes back as typed calls. Images and PDFs ride as inline ``blob`` attachments.
* **One conversation per Copilot session.** Inside an agent loop the conversation is
  kept (:mod:`clio_agent.providers.claude_code_conversation`): an append-only call
  continues the session with only the new messages; anything else starts a new session
  with the whole transcript, with a typed reset reason on the ``provider.stateful``
  audit row. Calls outside a loop run on the session's side entry.
* **Streaming.** Reasoning deltas as thinking, text deltas held back at the tool-call
  block, then the calls, then usage.
* **Errors.** Transport failures and stalls raise ``dspy.lm15`` types, so DSPy owns
  retries; a refused sign-in, a missing seat, a policy-disabled model and an exhausted
  premium allowance raise the typed errors in :mod:`.errors` (never retried).
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncGenerator, Iterator
from contextlib import aclosing
from typing import Any

from dspy.lm15 import (
    DocumentPart,
    FunctionTool,
    ImagePart,
    Request,
    Response,
    ServerError,
    StreamDeltaEvent,
    StreamStartEvent,
    ThinkingDelta,
)
from dspy.lm15 import TimeoutError as LMTimeoutError

from clio_agent.lm.engines.conversations import conversation_key
from clio_agent.lm.engines.text_tools import (
    ToolSchemas,
    callable_tools,
    render_messages,
    render_system,
    turn_reminder,
)
from clio_agent.providers.claude_code_clear import side_key
from clio_agent.providers.claude_code_conversation import Send, plan, record_sent
from clio_agent.providers.claude_code_conversation import reset as reset_conversation
from clio_agent.providers.claude_code_cwd import session_cwd
from clio_agent.providers.claude_code_engine import (
    _assemble,
    _next_call_index,
    _reset,
    _Turn,
)
from clio_agent.providers.claude_code_multimodal import native_blocks
from clio_agent.providers.copilot.errors import CopilotError, sdk_transport_errors
from clio_agent.providers.copilot.sessions import (
    DEAD_ENTRY_MARKER,
    SESSION_POOL,
    active_gact_session_id,
    event_type,
    require_copilot_sdk,
)
from clio_agent.providers.stateful_common import stateful_reset_payload

__all__ = ["AsyncCopilotEngine", "CopilotEngine", "blob_attachments"]

logger = logging.getLogger(__name__)

PROVIDER_LABEL = "copilot_sdk"
#: Events that carry nothing a call returns (bookkeeping the runtime always emits).
_QUIET_EVENTS = frozenset(
    {
        "assistant.turn_start",
        "assistant.turn_end",
        "assistant.intent",
        "assistant.streaming_delta",
        "user.message",
        "pending_messages.modified",
        "session.usage_info",
        "session.quota_observation",
        "session.info",
        "session.model_change",
    }
)
_COMPACTION_EVENTS = frozenset({"session.compaction_complete", "session.truncation"})


class AsyncCopilotEngine:
    """Async GitHub Copilot engine (see the module docstring)."""

    supports_function_calling = True
    supports_reasoning = True
    tool_protocol = "text_block"
    #: OpenAI-style usage: the runtime's ``input_tokens`` counts the whole prompt,
    #: cache writes included (live: 15059 input with 15049 written).
    input_tokens_exclude_cache = False

    def __init__(
        self,
        model: str,
        *,
        reasoning_effort: str | None = None,
        cwd: str | None = None,
        idle_timeout_s: float | None = None,
    ) -> None:
        self.model = model
        self.reasoning_effort = reasoning_effort
        self.cwd = cwd  # None: the session's workspace
        self.idle_timeout_s = idle_timeout_s

    def idle_timeout(self) -> float:
        """The longest gap between two events (``limits.lm_inter_token_idle_s`` by default)."""
        if self.idle_timeout_s is not None:
            return self.idle_timeout_s
        from clio_agent.runtime.lm_activity import inter_token_idle_seconds  # noqa: PLC0415

        return inter_token_idle_seconds()

    async def complete(self, request: Request) -> Response:
        """Run one send and return the assembled response."""
        return _assemble(request, [event async for event in self.stream(request)])

    async def stream(self, request: Request) -> AsyncGenerator[Any, None]:
        """Run one send, yielding lm15 stream events."""
        system = render_system(request.system, request.tools)
        cwd = self.cwd or session_cwd(active_gact_session_id())
        identity = (self.model, cwd, self.reasoning_effort or "")
        kept = conversation_key(*identity) is not None
        async for event in self._query(request, system, cwd, identity, kept):
            yield event

    async def aclose(self) -> None:
        """Nothing to release: the pooled runtimes belong to their GACT sessions."""

    async def _query(
        self, request: Request, system: str, cwd: str, identity: tuple[str, ...], kept: bool
    ) -> AsyncGenerator[Any, None]:
        await asyncio.to_thread(require_copilot_sdk)
        gact_session = active_gact_session_id()
        pool_key = gact_session if kept else side_key(gact_session)
        entry = SESSION_POOL.entry_for(pool_key)
        send = plan(entry, request, system, identity, kept=kept)
        body, blobs = _payload(request, send)  # typed refusal before any send
        handle = send.handle or send.call_id
        _audit_stateful(send, self.model, len(request.messages), handle, pool_key)
        call_index = _next_call_index()
        _audit_call("provider.call_started", send.call_id, call_index, self.model, chars=len(body))
        turn = _CopilotTurn(
            self.model,
            call_index,
            handle,
            available_tools=callable_tools(
                [tool.name for tool in request.tools if isinstance(tool, FunctionTool)]
            ),
            schemas=ToolSchemas(
                {
                    tool.name: tool.parameters
                    for tool in request.tools
                    if isinstance(tool, FunctionTool)
                }
            ),
        )
        yield StreamStartEvent(model=request.model)
        received = False
        try:
            for attempt in range(2):
                try:
                    async with aclosing(
                        entry.stream(
                            payload=body,
                            attachments=blobs,
                            handle=handle,
                            fresh=send.handle is None,
                            model=self.model,
                            system_prompt=system,
                            cwd=cwd,
                            reasoning_effort=self.reasoning_effort,
                            idle_timeout=self.idle_timeout(),
                            on_construct=SESSION_POOL.bump_construct,
                        )
                    ) as events:
                        async for event in events:
                            received = True
                            for out in turn.handle(event):
                                yield out
                            if turn.protocol_error:
                                break
                    break
                except RuntimeError as exc:
                    # The entry was released after this call took it and before anything
                    # was sent: take the session's fresh entry once and plan again (full).
                    if received or attempt or DEAD_ENTRY_MARKER not in str(exc):
                        raise
                    logger.info(
                        "copilot query reconnected reason=dead_entry_reconnect model=%s session=%s",
                        self.model,
                        gact_session,
                    )
                    entry = SESSION_POOL.entry_for(pool_key)
                    send = plan(entry, request, system, identity, kept=kept)
                    body, blobs = _payload(request, send)
                    handle = send.handle or send.call_id
        except CopilotError:
            _reset(entry, send)
            raise
        except TimeoutError as exc:
            _reset(entry, send)
            raise LMTimeoutError(f"{exc} (model={self.model})") from exc
        except (ConnectionError, OSError, EOFError) as exc:
            _reset(entry, send)
            raise ServerError(
                f"copilot runtime transport failed mid-stream (model={self.model}): "
                f"{type(exc).__name__}: {exc}"
            ) from exc
        except BaseException as exc:
            _reset(entry, send)
            if _is_sdk_transport_error(exc):
                raise ServerError(
                    f"copilot runtime transport failed mid-stream (model={self.model}): "
                    f"{type(exc).__name__}: {exc}"
                ) from exc
            raise
        finally:
            _audit_call(
                "provider.call_usage",
                send.call_id,
                call_index,
                self.model,
                chars=len(turn.reply),
                usage=turn.usage,
            )
        closing = turn.finish(send.call_id)
        # A one-shot session is replaced by the next one-shot call: nothing to record.
        if send.kept and turn.protocol_error:
            _reset(entry, send)
        elif send.kept and turn.provider_compacted:
            reset_conversation(entry, "provider_compacted")
        elif send.kept:
            record_sent(entry, send, handle, request, system)
        for out in closing:
            yield out


def _is_sdk_transport_error(exc: BaseException) -> bool:
    """The SDK's own JSON-RPC and process-exit errors (typed, never parsed from prose)."""
    return isinstance(exc, sdk_transport_errors())


def blob_attachments(parts: list[Any]) -> list[dict[str, Any]]:
    """Copilot ``blob`` attachments for image and PDF parts, bounded like Claude's blocks.

    Raises:
        ValueError: An unsupported media type, or a remote URL source (a blob is inline).
        NativeAttachmentTooLargeError: Over the shared per-kind or per-request ceiling.
    """
    blobs: list[dict[str, Any]] = []
    for index, block in enumerate(native_blocks(parts)):
        source = block["source"]
        if source.get("type") != "base64":
            raise ValueError(
                "GitHub Copilot takes inline attachments only; a remote "
                f"{block['type']} URL cannot be sent"
            )
        blobs.append(
            {
                "type": "blob",
                "data": source["data"],
                "mimeType": source["media_type"],
                "displayName": f"{block['type']}-{index + 1}",
            }
        )
    return blobs


def _payload(request: Request, send: Send) -> tuple[str, list[dict[str, Any]]]:
    body, media = render_messages(send.messages, media=(ImagePart, DocumentPart))
    if request.tools:
        body = f"{body}{chr(10) * 2}{turn_reminder()}"
    return body, blob_attachments(media)


class CopilotEngine(AsyncCopilotEngine):
    """Sync twin: drives the async send on this thread's private event loop."""

    def complete(self, request: Request) -> Response:  # type: ignore[override]
        return _assemble(request, list(self.stream(request)))

    def stream(self, request: Request) -> Iterator[Any]:  # type: ignore[override]
        loop = asyncio.new_event_loop()
        events = AsyncCopilotEngine.stream(self, request)
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
        """Nothing to release: the pooled runtimes belong to their GACT sessions."""


class _CopilotTurn(_Turn):
    """Folds Copilot session events into lm15 deltas, usage and the final reply."""

    label = "github copilot sdk"

    def handle(self, message: Any) -> list[Any]:
        if self.protocol_error:
            return []
        self.events += 1
        kind = event_type(message)
        data = getattr(message, "data", None)
        out: list[Any] = []
        if kind == "assistant.reasoning_delta":
            if text := getattr(data, "delta_content", ""):
                out.append(StreamDeltaEvent(delta=ThinkingDelta(text=text, part_index=0)))
        elif kind == "assistant.message_delta":
            if getattr(data, "parent_tool_call_id", None) is None and (
                text := getattr(data, "delta_content", "")
            ):
                self._on_text(text, out)
        elif kind == "assistant.message":
            if getattr(data, "parent_tool_call_id", None) is None:
                self.fallback = str(getattr(data, "content", "") or "") or self.fallback
        elif kind == "assistant.usage":
            self._note_copilot_usage(data)
        elif kind in _COMPACTION_EVENTS:
            self.provider_compacted = True
            _audit_event("provider.compacted", self.model, kind)
        elif kind not in _QUIET_EVENTS:
            _audit_event("provider.message_unhandled", self.model, kind)
        return out

    def _note_copilot_usage(self, data: Any) -> None:
        usage = {
            "input_tokens": getattr(data, "input_tokens", None),
            "output_tokens": getattr(data, "output_tokens", None),
            "cache_read_input_tokens": getattr(data, "cache_read_tokens", None),
            "cache_creation_input_tokens": getattr(data, "cache_write_tokens", None),
            "reasoning_tokens": getattr(data, "reasoning_tokens", None),
            # Premium requests this call consumed (the model's multiplier), not dollars.
            "premium_requests": getattr(data, "cost", None),
        }
        self.usage = {key: value for key, value in usage.items() if value is not None}
        if getattr(data, "finish_reason", None) == "length":
            self.stop_reason = "max_tokens"


def _audit_event(stage: str, model: str, kind: str) -> None:
    """Record a session event the engine does not turn into output (never a silent drop)."""
    from clio_agent.runtime.stream_audit import stream_audit  # noqa: PLC0415

    stream_audit(stage, provider=PROVIDER_LABEL, model=model, message_type=kind, subtype="")


def _audit_call(
    stage: str,
    call_id: str,
    call_index: int,
    model: str,
    *,
    chars: int,
    usage: dict[str, Any] | None = None,
) -> None:
    from clio_agent.runtime.stream_audit import stream_audit, stream_audit_enabled  # noqa: PLC0415

    if not stream_audit_enabled():
        return
    row: dict[str, Any] = {
        "provider": PROVIDER_LABEL,
        "transport": "sdk",
        "model": model,
        "call_id": call_id,
        "call_index": call_index,
    }
    if usage is None:
        row["prompt_chars"] = chars
    else:
        row.update(output_chars=chars, usage=json.loads(json.dumps(usage, default=str)))
    stream_audit(stage, **row)


def _audit_stateful(send: Send, model: str, total: int, handle: str, conversation: str) -> None:
    """One ``provider.stateful`` audit row per call inside an agent loop."""
    from clio_agent.runtime.stream_audit import stream_audit, stream_audit_enabled  # noqa: PLC0415

    if not send.kept or not stream_audit_enabled():
        return
    row: dict[str, Any] = {
        "provider": PROVIDER_LABEL,
        "transport": "sdk",
        "model": f"copilot/{model}",
        "call_id": send.call_id,
        "stateful_mode": "delta" if send.handle else "full",
        "total_messages": total,
        "sent_messages": len(send.messages),
        "conversation": conversation,
        "session_id": handle,
    }
    if send.reason is not None:
        row.update(stateful_reset_payload(send.reason))
    stream_audit("provider.stateful", **row)
