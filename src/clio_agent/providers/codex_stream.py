"""Official Python Codex SDK transport for the subscription provider.

CLIO imports :mod:`openai_codex` and consumes its typed turn stream. The SDK
owns the pinned runtime and JSON-RPC lifecycle; CLIO never shells out to
``codex``, speaks app-server JSON-RPC, or falls back to a CLI transport.

Provider-exposed reasoning text and reasoning summaries remain distinct. A
summary is never relabelled as full provider reasoning.

The official SDK owns its pinned runtime, subprocess, and thread state. That
gives CLIO one typed cancellation path and removes the unsupported shell/app-
server transports, at the measured cost of roughly 2.5x first-token latency.
Progress is consequently bounded per SDK exchange, never by a composite turn
deadline that could kill a healthy long-running stream.
"""

from __future__ import annotations

import asyncio
import atexit
import logging
import queue
import re
import tempfile
import threading
import uuid
from collections.abc import AsyncIterator, Mapping
from typing import Any

from openai_codex import ApprovalMode, AsyncCodex, CodexConfig, CodexError, Sandbox
from openai_codex.types import ReasoningEffort, ReasoningSummary

from clio_agent.providers._cli_provider import raise_model_rejected
from clio_agent.providers.claude_code_cancel import (
    register_sdk_stream,
    unregister_sdk_stream,
)
from clio_agent.providers.codex_audit import (
    emit_call_started,
    emit_call_usage,
    emit_normalized,
    emit_raw_event,
)
from clio_agent.providers.codex_credential_home import IsolatedCodexHome

logger = logging.getLogger(__name__)

DEFAULT_SDK_PROGRESS_TIMEOUT_S = 120.0
DEFAULT_TURN_TIMEOUT_S = 180.0


async def _cleanup_sdk_action(action: str, awaitable: Any) -> None:
    """Await one SDK teardown action and report failure without masking the turn."""
    try:
        await awaitable
    except Exception as exc:  # noqa: BLE001 - cleanup is typed and observable
        logger.warning(
            "Codex SDK cleanup failed reason=codex_sdk_cleanup_failed action=%s error=%r",
            action,
            exc,
        )


BARE_LM_BASE_INSTRUCTIONS = """You are a language-model completion backend inside Clio.
Answer only the serialized prompt supplied by Clio. Do not inspect the workspace,
invoke Codex tools, delegate to agents, browse, use plugins, or perform work outside
the prompt. Clio owns the agent loop and all tool execution. Follow the response
contract in the prompt and return its requested assistant content directly."""

BARE_LM_FEATURES: dict[str, bool] = {
    "apps": False,
    "browser_use": False,
    "computer_use": False,
    "image_generation": False,
    "memories": False,
    "multi_agent": False,
    "shell_tool": False,
    "view_image": False,
    "workspace_dependencies": False,
}
BARE_LM_CONFIG_OVERRIDES = (
    "mcp_servers={}",
    "plugins={}",
    *(f"features.{name}=false" for name in BARE_LM_FEATURES),
)
BARE_LM_THREAD_CONFIG: dict[str, Any] = {
    "mcp_servers": {},
    "plugins": {},
    "features": BARE_LM_FEATURES,
}

_ALLOWED_ITEM_TYPES = frozenset({"agentMessage", "reasoning", "userMessage"})
_ACTION_ITEM_TYPES = frozenset(
    {
        "collabAgentToolCall",
        "commandExecution",
        "dynamicToolCall",
        "fileChange",
        "imageGeneration",
        "mcpToolCall",
        "subAgentActivity",
        "webSearch",
    }
)
_MODEL_ACTIVITY_METHODS = frozenset(
    {
        "item/agentMessage/delta",
        "item/reasoning/textDelta",
        "item/reasoning/summaryTextDelta",
        "item/reasoning/summaryPartAdded",
    }
)
_STREAM_END = object()
_CALL_COUNTER_LOCK = threading.Lock()
_CALL_COUNTER = 0
_CODEX_MODEL_REJECTION_PATTERN = re.compile(
    r"is not supported when using codex with (a|an)\b[^.]{0,40}account",
    re.IGNORECASE,
)


class CodexSDKError(RuntimeError):
    """Typed failure raised by the sole Codex SDK provider transport."""


def _sdk_progress_timeout_s(requested_timeout: float) -> float:
    """Resolve the maximum silence allowed for one SDK exchange or event."""
    from clio_agent import conf  # noqa: PLC0415

    configured = conf.resolve(
        "limits.codex_sdk_progress_timeout_s",
        env="CLIO_CODEX_SDK_PROGRESS_TIMEOUT_S",
        default=DEFAULT_SDK_PROGRESS_TIMEOUT_S,
        cast=conf.as_float,
    )
    return max(0.01, min(float(requested_timeout), float(configured)))


def _next_call_index() -> int:
    """Return a process-local Codex provider call index for audit correlation."""
    global _CALL_COUNTER  # noqa: PLW0603
    with _CALL_COUNTER_LOCK:
        _CALL_COUNTER += 1
        return _CALL_COUNTER


def _is_codex_model_rejection(text: str, *, model: str) -> bool:
    """Return whether ``text`` is the verified account/model rejection shape."""
    return bool(text and model and model in text and _CODEX_MODEL_REJECTION_PATTERN.search(text))


def usage_chunk(usage: dict[str, int] | None) -> dict[str, int] | None:
    """Map normalized SDK usage to the LiteLLM streaming usage shape."""
    if not usage:
        return None
    prompt_tokens = int(usage.get("input_tokens", 0) or 0)
    completion_tokens = int(usage.get("output_tokens", 0) or 0)
    total = int(usage.get("total_tokens", 0) or 0) or prompt_tokens + completion_tokens
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "reasoning_output_tokens": int(usage.get("reasoning_output_tokens", 0) or 0),
        "total_tokens": total,
    }


def _stream_chunk(
    *,
    text: str,
    is_finished: bool,
    finish_reason: str | None = None,
    usage: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build one LiteLLM-compatible streaming chunk."""
    return {
        "text": text,
        "is_finished": is_finished,
        "finish_reason": finish_reason or ("stop" if is_finished else None),
        "index": 0,
        "tool_use": None,
        "usage": usage,
    }


def _item_root(payload: Any) -> Any:
    item = getattr(payload, "item", None)
    return getattr(item, "root", item)


def _item_type(payload: Any) -> str:
    return str(getattr(_item_root(payload), "type", "") or "")


def _is_model_activity(event: Any) -> bool:
    if str(getattr(event, "method", "")) in _MODEL_ACTIVITY_METHODS:
        return True
    return str(getattr(event, "method", "")) == "item/started" and _item_type(event.payload) in {
        "agentMessage",
        "reasoning",
    }


def _validate_bare_lm_event(event: Any) -> None:
    """Reject any SDK item proving Codex started an invisible inner agent action."""
    method = str(getattr(event, "method", ""))
    if method not in {"item/started", "item/completed"}:
        return
    item_type = _item_type(event.payload) or "unknown"
    if item_type in _ACTION_ITEM_TYPES or any(
        marker in item_type.lower()
        for marker in ("toolcall", "commandexecution", "filechange", "subagent")
    ):
        raise CodexSDKError(
            "bare Codex SDK LM attempted a hidden internal action "
            f"({item_type}); Clio owns tools and orchestration"
        )
    if item_type not in _ALLOWED_ITEM_TYPES:
        logger.info(
            "Codex SDK informational item skipped "
            "reason=codex_sdk_informational_item_skipped item_type=%s",
            item_type,
        )


def _normalize_usage(payload: Any) -> dict[str, int]:
    last = getattr(getattr(payload, "token_usage", None), "last", None)
    if last is None:
        return {}
    return {
        "input_tokens": int(getattr(last, "input_tokens", 0) or 0),
        "cached_input_tokens": int(getattr(last, "cached_input_tokens", 0) or 0),
        "cache_write_input_tokens": int(getattr(last, "cache_write_input_tokens", 0) or 0),
        "output_tokens": int(getattr(last, "output_tokens", 0) or 0),
        "reasoning_output_tokens": int(getattr(last, "reasoning_output_tokens", 0) or 0),
        "total_tokens": int(getattr(last, "total_tokens", 0) or 0),
    }


def _raise_failed_turn(event: Any) -> None:
    if str(getattr(event, "method", "")) != "turn/completed":
        return
    turn = getattr(event.payload, "turn", None)
    status = getattr(getattr(turn, "status", None), "value", getattr(turn, "status", ""))
    if str(status) != "failed":
        return
    error = getattr(turn, "error", None)
    message = str(getattr(error, "message", "") or "Codex SDK turn failed")
    raise CodexSDKError(message)


class CodexSDKClient:
    """Persistent official SDK client hosted on a private event-loop thread."""

    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._client: AsyncCodex | None = None
        self._client_lock: asyncio.Lock | None = None
        self._sdk_home: IsolatedCodexHome | None = None
        # The SDK client is a process-wide singleton shared by every concurrent codex
        # turn. ``_generation`` identifies which client a pump is holding, ``_client_users``
        # counts the pumps still holding it, and ``_reset_pending`` records a teardown
        # requested by a failed turn but not yet safe to perform.
        self._generation = 0
        self._client_users = 0
        self._reset_pending = False

    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        with self._guard:
            if self._loop is not None:
                return self._loop
            loop = asyncio.new_event_loop()
            thread = threading.Thread(target=loop.run_forever, name="codex-sdk-loop", daemon=True)
            thread.start()
            self._loop, self._thread = loop, thread
            return loop

    async def _ensure_client(self) -> AsyncCodex:
        if self._client_lock is None:
            self._client_lock = asyncio.Lock()
        async with self._client_lock:
            if self._client is None:
                sdk_home = IsolatedCodexHome()
                started = False
                try:
                    client = AsyncCodex(
                        CodexConfig(
                            cwd=tempfile.gettempdir(),
                            config_overrides=BARE_LM_CONFIG_OVERRIDES,
                            env=sdk_home.start(),
                            client_name="clio_agent",
                            client_title="CLIO Agent",
                        )
                    )
                    await client.__aenter__()
                    started = True
                finally:
                    # ``__aenter__`` spawns the runtime subprocess and runs the JSON-RPC
                    # handshake, so a Stop or the startup progress deadline lands here as
                    # ``CancelledError`` -- a ``BaseException`` an ``except Exception``
                    # cleanup cannot see. The leaked home would strand a 0600 copy of the
                    # user's credentials and permanently consume one live-home slot.
                    if not started:
                        sdk_home.close()
                self._client = client
                self._sdk_home = sdk_home
                self._generation += 1
                self._client_users = 0
                self._reset_pending = False
        assert self._client is not None
        return self._client

    async def _reset_client(self) -> None:
        """Close the shared SDK client unconditionally (process teardown only)."""
        if self._client_lock is None:
            self._client_lock = asyncio.Lock()
        async with self._client_lock:
            await self._close_locked()

    async def _close_locked(self) -> None:
        """Close and forget the current client. The caller must hold ``_client_lock``."""
        client, self._client = self._client, None
        sdk_home, self._sdk_home = self._sdk_home, None
        self._client_users = 0
        self._reset_pending = False
        self._generation += 1
        try:
            if client is not None:
                await _cleanup_sdk_action("client_close", client.close())
        finally:
            if sdk_home is not None:
                sdk_home.close()

    async def _release_client(self, generation: int, *, reset: bool) -> None:
        """Drop one turn's hold on the shared client, tearing it down only when safe.

        ``client.close()`` terminates the single ``codex`` runtime subprocess, which
        fails every other in-flight turn's stream with a transport error attributed to
        its own turn. A teardown requested by a failing turn is therefore recorded and
        executed once the LAST holder of that client generation has left.

        Args:
            generation: The client generation the releasing pump obtained.
            reset: Whether this pump's turn ended in a state that should discard the
                shared client.
        """

        if self._client_lock is None:
            self._client_lock = asyncio.Lock()
        async with self._client_lock:
            if generation != self._generation:
                return
            self._client_users = max(0, self._client_users - 1)
            if reset:
                self._reset_pending = True
            if self._reset_pending and self._client_users == 0:
                await self._close_locked()
            elif reset:
                logger.info(
                    "Codex SDK client teardown deferred while other turns hold it "
                    "reason=codex_sdk_reset_deferred_in_flight holders=%d",
                    self._client_users,
                )

    async def stream(
        self,
        *,
        prompt: str,
        model: str,
        cwd: str | None,
        effort: ReasoningEffort | None,
        timeout: float,
    ) -> AsyncIterator[Any]:
        """Bridge one typed SDK turn stream from the owner loop to the caller loop."""
        owner_loop = self._ensure_loop()
        caller_loop = asyncio.get_running_loop()
        chunks: queue.SimpleQueue[tuple[Any, Any]] = queue.SimpleQueue()

        async def _pump() -> None:
            turn = None
            stream = None
            clean = False
            cancelled = False
            generation = -1
            try:
                progress_timeout = _sdk_progress_timeout_s(timeout)

                def _record(event: Any) -> None:
                    _validate_bare_lm_event(event)
                    _raise_failed_turn(event)
                    chunks.put(("event", event))

                async def _await_progress(awaitable: Any, *, phase: str) -> Any:
                    try:
                        return await asyncio.wait_for(awaitable, timeout=progress_timeout)
                    except TimeoutError as exc:
                        raise CodexSDKError(
                            "Codex SDK made no progress for "
                            f"{progress_timeout:g}s during {phase} "
                            "reason=codex_sdk_progress_timeout"
                        ) from exc

                client = await _await_progress(self._ensure_client(), phase="client startup")
                # Claim a hold on this client generation. Every pump runs on the single
                # owner loop, so recording the hold in the same synchronous block as the
                # await's return is atomic with respect to the other pumps.
                generation = self._generation
                self._client_users += 1
                thread = await _await_progress(
                    client.thread_start(
                        approval_mode=ApprovalMode.deny_all,
                        base_instructions=BARE_LM_BASE_INSTRUCTIONS,
                        config=BARE_LM_THREAD_CONFIG,
                        cwd=cwd or tempfile.gettempdir(),
                        developer_instructions=BARE_LM_BASE_INSTRUCTIONS,
                        ephemeral=True,
                        model=model,
                        sandbox=Sandbox.read_only,
                    ),
                    phase="thread start",
                )
                turn = await _await_progress(
                    thread.turn(
                        prompt,
                        effort=effort,
                        summary=ReasoningSummary.model_validate("detailed"),
                    ),
                    phase="turn start",
                )
                stream = turn.stream()

                while True:
                    try:
                        assert stream is not None
                        event = await _await_progress(anext(stream), phase="event stream")
                    except StopAsyncIteration:
                        break
                    _record(event)
                clean = True
            except asyncio.CancelledError:
                cancelled = True
                if turn is not None:
                    await _cleanup_sdk_action("turn_interrupt_cancel", turn.interrupt())
                raise
            except BaseException as exc:  # noqa: BLE001 - delivered to caller loop
                if turn is not None:
                    await _cleanup_sdk_action("turn_interrupt_error", turn.interrupt())
                chunks.put(("exc", exc))
            finally:
                if stream is not None:
                    close_stream = getattr(stream, "aclose", None)
                    if callable(close_stream):
                        await _cleanup_sdk_action("stream_close", close_stream())
                if generation >= 0:
                    # Only a pump that actually obtained the client may ask for its
                    # teardown; a failure BEFORE the handshake completed would otherwise
                    # close a client that belongs entirely to other turns.
                    await self._release_client(generation, reset=not clean and not cancelled)
                chunks.put((_STREAM_END, None))

        future = asyncio.run_coroutine_threadsafe(_pump(), owner_loop)
        try:
            from clio_agent.gact.context import active_session_id  # noqa: PLC0415

            gact_sid = active_session_id() or ""
        except Exception:  # noqa: BLE001 - off-turn SDK calls are not cancellable by session
            gact_sid = ""

        def _cancel_future() -> None:
            future.cancel()

        handle = register_sdk_stream(gact_sid, _cancel_future)
        try:
            while True:
                kind, value = await caller_loop.run_in_executor(None, chunks.get)
                if kind is _STREAM_END:
                    break
                if kind == "exc":
                    raise value
                yield value
        finally:
            unregister_sdk_stream(handle)
            if not future.done():
                future.cancel()

    def close_blocking(self) -> None:
        """Close the SDK-owned runtime and stop the owner loop."""
        with self._guard:
            loop, thread = self._loop, self._thread
            self._loop, self._thread = None, None
        if loop is None:
            return
        try:
            if loop.is_running():
                asyncio.run_coroutine_threadsafe(self._reset_client(), loop).result(timeout=15)
        except Exception:  # noqa: BLE001 - teardown is best effort and logged
            logger.warning("Codex SDK client teardown failed", exc_info=True)
        finally:
            loop.call_soon_threadsafe(loop.stop)
            if thread is not None:
                thread.join(timeout=15)


_SDK_CLIENT = CodexSDKClient()
atexit.register(_SDK_CLIENT.close_blocking)


def _note_provider_thinking(text: str, *, summary: bool) -> None:
    if not text:
        return
    try:
        from clio_agent.runtime.lm_activity import note_lm_provider_thinking_delta

        provider = "codex_sdk_summary" if summary else "codex_sdk_reasoning"
        note_lm_provider_thinking_delta(text, provider=provider)
    except Exception:  # noqa: BLE001,S110 - observability must not break the turn
        pass


async def astream_sdk(
    *,
    prompt: str,
    model: str,
    cwd: str | None,
    effort: ReasoningEffort | None,
    timeout: float,
    call_index: int,
) -> AsyncIterator[dict[str, Any]]:
    """Stream one official SDK turn into LiteLLM chunks and CLIO thinking lanes."""
    call_id = uuid.uuid4().hex
    emit_call_started(call_id=call_id, call_index=call_index, model=model, prompt=prompt)
    final_text = ""
    fallback_text = ""
    usage: dict[str, int] = {}
    event_index = 0
    summary_parts = 0
    try:
        async for event in _SDK_CLIENT.stream(
            prompt=prompt,
            model=model,
            cwd=cwd,
            effort=effort,
            timeout=timeout,
        ):
            event_index += 1
            method = str(getattr(event, "method", ""))
            payload = event.payload
            if method == "item/agentMessage/delta":
                text = str(getattr(payload, "delta", "") or "")
                if not text:
                    continue
                final_text += text
                emit_raw_event(
                    call_index=call_index,
                    event_index=event_index,
                    source_channel="text_delta",
                    text=text,
                    raw_event_type=method,
                )
                emit_normalized(
                    call_index=call_index,
                    event_index=event_index,
                    source_channel="text_delta",
                    normalized_event="contract.content",
                    text=text,
                )
                yield _stream_chunk(text=text, is_finished=False)
            elif method == "item/reasoning/summaryPartAdded":
                if summary_parts:
                    boundary = "\n\n"
                    emit_raw_event(
                        call_index=call_index,
                        event_index=event_index,
                        source_channel="reasoning_summary",
                        text=boundary,
                        raw_event_type=method,
                    )
                    _note_provider_thinking(boundary, summary=True)
                summary_parts += 1
            elif method in {
                "item/reasoning/textDelta",
                "item/reasoning/summaryTextDelta",
            }:
                text = str(getattr(payload, "delta", "") or "")
                is_summary = method.endswith("summaryTextDelta")
                source = "reasoning_summary" if is_summary else "reasoning_text"
                emit_raw_event(
                    call_index=call_index,
                    event_index=event_index,
                    source_channel=source,
                    text=text,
                    raw_event_type=method,
                )
                _note_provider_thinking(text, summary=is_summary)
            elif method == "thread/tokenUsage/updated":
                usage = _normalize_usage(payload) or usage
            elif method == "item/completed":
                item = _item_root(payload)
                if str(getattr(item, "type", "")) == "agentMessage":
                    phase_value = getattr(item, "phase", None)
                    phase = getattr(phase_value, "value", phase_value)
                    text = str(getattr(item, "text", "") or "")
                    if phase == "final_answer" or not fallback_text:
                        fallback_text = text
    except CodexError as exc:
        message = str(exc)
        if _is_codex_model_rejection(message, model=model):
            raise_model_rejected(
                message=f"codex rejected model {model!r}: {message}",
                model=f"codex/{model}",
                llm_provider="codex",
                cause=exc,
            )
        raise CodexSDKError(f"Codex SDK stream failed (model={model}): {exc}") from exc
    finally:
        emit_call_usage(
            call_id=call_id,
            call_index=call_index,
            model=model,
            usage=usage,
            output_chars=len(final_text or fallback_text),
        )
    if not final_text and fallback_text:
        final_text = fallback_text
        yield _stream_chunk(text=fallback_text, is_finished=False)
    if not final_text:
        raise CodexSDKError(f"Codex SDK returned empty content (model={model})")
    yield _stream_chunk(
        text="",
        is_finished=True,
        finish_reason="stop",
        usage=usage_chunk(usage),
    )


def run_sdk(
    *,
    prompt: str,
    model: str,
    cwd: str | None = None,
    effort: ReasoningEffort | None = None,
    timeout: float = DEFAULT_TURN_TIMEOUT_S,
    call_index: int = 0,
) -> tuple[str, dict[str, int]]:
    """Collect one official SDK stream for LiteLLM's blocking completion path."""

    async def _collect() -> tuple[str, dict[str, int]]:
        parts: list[str] = []
        final_usage: dict[str, int] = {}
        async for chunk in astream_sdk(
            prompt=prompt,
            model=model,
            cwd=cwd,
            effort=effort,
            timeout=timeout,
            call_index=call_index,
        ):
            parts.append(str(chunk.get("text") or ""))
            raw_usage = chunk.get("usage")
            if isinstance(raw_usage, dict):
                final_usage = {
                    "input_tokens": int(raw_usage.get("prompt_tokens", 0) or 0),
                    "output_tokens": int(raw_usage.get("completion_tokens", 0) or 0),
                    "reasoning_output_tokens": int(
                        raw_usage.get("reasoning_output_tokens", 0) or 0
                    ),
                    "total_tokens": int(raw_usage.get("total_tokens", 0) or 0),
                }
        return "".join(parts), final_usage

    return asyncio.run(_collect())


__all__ = [
    "CodexSDKClient",
    "CodexSDKError",
    "DEFAULT_SDK_PROGRESS_TIMEOUT_S",
    "_SDK_CLIENT",
    "_next_call_index",
    "astream_sdk",
    "run_sdk",
    "usage_chunk",
]
