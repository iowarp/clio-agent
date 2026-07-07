"""LiteLLM ``CustomLLM`` provider for the Claude Code CLI.

Routes ``dspy.LM(model="claude_code/<model>", ...)`` calls through
``claude -p`` so CLIO can use the user's Claude Code subscription auth
without bypassing the DSPy/LiteLLM provider contract.

The Claude Code process is used as a bare model transport. Built-in tools
are disabled with ``--tools ""``; CLIO's planner and MCP/tool gateway remain
the only tool execution layer.
"""

from __future__ import annotations

import asyncio
import atexit
import json
import logging
import os
import shutil
import subprocess
import threading
import time
import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any

from clio_agent.providers._cli_provider import (
    messages_to_prompt,
    register_custom_provider,
)
from clio_agent.runtime import trace
from clio_agent.runtime.stream_audit import stream_audit

logger = logging.getLogger(__name__)

try:
    from litellm import CustomLLM
    from litellm.types.utils import Choices, Message, ModelResponse, Usage
except ImportError as e:  # pragma: no cover - litellm is a hard dep
    raise ImportError("litellm must be installed to use the Claude Code provider") from e


CLAUDE_BINARY_NAME = "claude"
# Default to the Claude Agent SDK transport (persistent CLI session, no per-call
# spawn, cleaner prompt isolation). "exec" (one `claude -p` per call) is the explicit
# opt-out via claude_code_transport / CLIO_CLAUDE_CODE_TRANSPORT.
DEFAULT_TRANSPORT = "sdk"


class ClaudeCodeCLIUnavailableError(RuntimeError):
    """Raised when the ``claude`` binary is unavailable."""


class ClaudeCodeExecError(RuntimeError):
    """Raised when ``claude -p`` fails or returns malformed output."""


class ClaudeCodeUnsupportedMultimodalError(ClaudeCodeExecError):
    """Raised when Claude Code CLI transport receives content it would drop."""


_CALL_COUNTER_LOCK = threading.Lock()
_CALL_COUNTER = 0


def _next_call_index() -> int:
    """Return a process-local Claude Code provider call index for trace logs."""

    global _CALL_COUNTER  # noqa: PLW0603
    with _CALL_COUNTER_LOCK:
        _CALL_COUNTER += 1
        return _CALL_COUNTER


def _trace_json(value: Any) -> str:
    """Serialize provider I/O for the existing trace logger."""

    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    except TypeError:
        return json.dumps(str(value), ensure_ascii=False)


def _active_gact_ids() -> tuple[str, str, str]:
    """Return active GACT session, turn, and trace ids for audit rows."""

    try:
        from clio_agent.gact.context import (  # noqa: PLC0415
            active_session_id,
            active_trace_id,
            active_turn_id,
        )

        return active_session_id(), active_turn_id(), active_trace_id()
    except Exception:  # noqa: BLE001 - provider audit must never break calls
        return "", "", ""


def _messages_to_claude_prompt(messages: list[dict[str, Any]]) -> str:
    """Serialize chat messages into role-hardened JSON Lines.

    Thin wrapper over the shared CLI-provider serializer
    (:func:`clio_agent.providers._cli_provider.messages_to_prompt`) with Claude
    Code's own unsupported-multimodal exception + transport label.
    """
    return messages_to_prompt(
        messages,
        unsupported_multimodal_exc=ClaudeCodeUnsupportedMultimodalError,
        transport_label="Claude Code",
    )


def _resolve_claude_binary() -> str:
    """Return an executable Claude Code binary path or raise."""
    if os.name == "nt":
        cmd_path = shutil.which(f"{CLAUDE_BINARY_NAME}.cmd")
        if cmd_path:
            return cmd_path
    path = shutil.which(CLAUDE_BINARY_NAME)
    if not path:
        raise ClaudeCodeCLIUnavailableError(
            "`claude` not found on PATH. Install Claude Code and run "
            "`claude login` once per machine."
        )
    return path


def _run_exec(
    *,
    prompt: str,
    model: str,
    timeout: float | None = 180.0,
    cwd: str | None = None,
    extra_args: list[str] | None = None,
    call_index: int = 0,
) -> tuple[str, dict[str, Any]]:
    """Run ``claude -p`` and return ``(text, usage)``."""
    binary = _resolve_claude_binary()
    argv = [
        binary,
        "-p",
        "--output-format",
        "json",
        "--input-format",
        "text",
        "--model",
        model,
        "--session-id",
        str(uuid.uuid4()),
        "--tools",
        "",
    ]
    if extra_args:
        argv.extend(extra_args)

    try:
        proc = subprocess.run(
            argv,
            input=prompt,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            cwd=cwd,
            check=False,
        )
    except subprocess.TimeoutExpired as e:
        raise ClaudeCodeExecError(f"claude -p timed out after {timeout}s (model={model})") from e

    output = (proc.stdout or "").strip()
    if proc.returncode != 0:
        raise ClaudeCodeExecError(
            f"claude -p returned {proc.returncode} for model={model}: "
            f"{(proc.stderr or output).strip()[:500]}"
        )
    try:
        payload = json.loads(output)
    except json.JSONDecodeError as e:
        raise ClaudeCodeExecError(
            f"claude -p returned malformed JSON for model={model}: {output[:500]}"
        ) from e

    if payload.get("is_error"):
        raise ClaudeCodeExecError(
            f"claude -p returned an error for model={model}: "
            f"{payload.get('api_error_status') or payload.get('subtype') or payload}"
        )
    text = str(payload.get("result") or "").strip()
    if not text:
        raise ClaudeCodeExecError(f"claude -p returned empty content (model={model})")
    usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
    session_id, turn_id, trace_id = _active_gact_ids()
    stream_audit(
        "provider.raw_event",
        provider="claude_code_exec",
        session_id=session_id,
        turn_id=turn_id,
        trace_id=trace_id,
        call_index=call_index,
        event_index=1,
        raw_event_type="claude_exec_result",
        source_channel="content",
        model=f"claude_code/{model}",
        transport="exec",
        text_len=len(text),
        chunk_len=len(text),
        usage_keys=sorted(str(key) for key in usage),
        head=text[:120],
    )
    stream_audit(
        "provider.batch_response",
        provider="claude_code_exec",
        session_id=session_id,
        turn_id=turn_id,
        trace_id=trace_id,
        call_index=call_index,
        source_channel="content",
        model=f"claude_code/{model}",
        transport="exec",
        content_len=len(text),
        reasoning_len=0,
        chunk_len=len(text),
        finish_reason="stop",
        head=text[:120],
    )
    return text, usage


class _SdkSession:
    """Process-wide persistent Claude Agent SDK session (#715).

    One ``ClaudeSDKClient`` CLI connection is opened once and reused across every LM
    call, so calls after the first avoid the ~10-15s cold start that the ``exec`` path
    (and a fresh ``query()``) pays. All SDK I/O runs on a single dedicated asyncio loop
    in a daemon thread; clio's worker threads submit coroutines via
    ``run_coroutine_threadsafe`` and block on the result, so concurrent calls are
    serialized onto the one connection (the client handles one query/receive cycle at a
    time). The connection is opened lazily and rebuilt when the bound model/cwd changes.

    Bare-model transport, mirroring :func:`_run_exec`: Claude Code's own tools are
    disabled, ``max_turns=1``, and ``setting_sources=[]`` so the model sees only clio's
    transcript (no ``~/.claude`` CLAUDE.md/settings) and clio's ReAct loop drives tools.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._loop: Any = None
        self._thread: threading.Thread | None = None
        self._client: Any = None
        self._model: str | None = None
        self._cwd: str | None = None

    def _ensure_loop(self) -> None:
        if self._loop is not None:
            return
        loop = asyncio.new_event_loop()
        thread = threading.Thread(target=loop.run_forever, name="claude-sdk-loop", daemon=True)
        thread.start()
        self._loop, self._thread = loop, thread
        atexit.register(self.close)

    def _submit(self, coro: Any, timeout: float | None) -> Any:
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout=timeout)

    async def _aconnect(self, model: str, cwd: str | None) -> Any:
        from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient  # noqa: PLC0415

        options = ClaudeAgentOptions(
            tools=[],
            model=model,
            max_turns=1,
            allowed_tools=[],
            permission_mode="bypassPermissions",
            setting_sources=[],
            cwd=cwd,
        )
        client = ClaudeSDKClient(options=options)
        await client.connect()
        return client

    async def _aquery(self, prompt: str) -> tuple[str, dict[str, Any]]:
        from claude_agent_sdk import (  # noqa: PLC0415
            AssistantMessage,
            ResultMessage,
            TextBlock,
        )

        await self._client.query(prompt, session_id=uuid.uuid4().hex)
        parts: list[str] = []
        usage: dict[str, Any] = {}
        async for msg in self._client.receive_response():
            if isinstance(msg, AssistantMessage):
                parts.extend(b.text for b in msg.content if isinstance(b, TextBlock))
            elif isinstance(msg, ResultMessage):
                u = getattr(msg, "usage", None)
                if isinstance(u, dict):
                    usage = u
        return "".join(parts).strip(), usage

    def _reset_client(self) -> None:
        if self._client is None:
            return
        try:
            self._submit(self._client.disconnect(), timeout=15.0)
        except Exception:  # noqa: BLE001 - best-effort teardown; never block the caller
            logger.warning("claude sdk client disconnect failed", exc_info=True)
        self._client = self._model = self._cwd = None

    def complete(
        self, *, prompt: str, model: str, timeout: float | None, cwd: str | None
    ) -> tuple[str, dict[str, Any]]:
        try:
            import claude_agent_sdk  # noqa: F401,PLC0415
        except ImportError as exc:
            raise ClaudeCodeCLIUnavailableError(
                "claude_code_transport='sdk' requires the claude-agent-sdk package "
                "(install the 'claude-code' extra)."
            ) from exc

        with self._lock:
            self._ensure_loop()
            if self._client is None or self._model != model or self._cwd != cwd:
                self._reset_client()
                self._client = self._submit(self._aconnect(model, cwd), timeout=60.0)
                self._model, self._cwd = model, cwd
            try:
                text, usage = self._submit(self._aquery(prompt), timeout=timeout)
            except TimeoutError as exc:
                # A timed-out call leaves the connection mid-cycle; drop it so the
                # next call reconnects cleanly.
                self._reset_client()
                raise ClaudeCodeExecError(
                    f"claude agent sdk timed out after {timeout}s (model={model})"
                ) from exc
        if not text:
            raise ClaudeCodeExecError(f"claude agent sdk returned empty content (model={model})")
        return text, usage

    def close(self) -> None:
        with self._lock:
            self._reset_client()
            if self._loop is not None:
                self._loop.call_soon_threadsafe(self._loop.stop)
                self._loop = None


class _SdkSessionPool:
    """Keyed pool of persistent Claude Agent SDK sessions (#818).

    Maps ``(model, cwd)`` to a dedicated :class:`_SdkSession`, so two experts that
    want *different* ``claude_code`` models can run concurrently — each holds its own
    CLI connection (and its own asyncio loop/thread) instead of thrashing one shared
    ``_SdkSession`` that would tear down and reconnect on every model/cwd flip.

    Same-key calls still share one session and serialize onto its single connection
    (the SDK client handles one query/receive cycle at a time); distinct-key calls
    never contend. The per-app profile store (#818) makes concurrent distinct
    ``claude_code`` experts *expressible*, and this pool makes them *safe*: the pool
    lock is held only for the O(1) session lookup/creation, never across a completion.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sessions: dict[tuple[str, str | None], _SdkSession] = {}

    def _session_for(self, model: str, cwd: str | None) -> _SdkSession:
        """Return (creating if needed) the session bound to ``(model, cwd)``."""

        key = (model, cwd)
        with self._lock:
            session = self._sessions.get(key)
            if session is None:
                session = _SdkSession()
                self._sessions[key] = session
            return session

    def complete(
        self, *, prompt: str, model: str, timeout: float | None, cwd: str | None
    ) -> tuple[str, dict[str, Any]]:
        """Complete one turn on the session keyed by ``(model, cwd)``."""

        return self._session_for(model, cwd).complete(
            prompt=prompt, model=model, timeout=timeout, cwd=cwd
        )

    def close(self) -> None:
        """Tear down every pooled session and drop the pool."""

        with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for session in sessions:
            session.close()


_SDK_SESSION_POOL = _SdkSessionPool()
atexit.register(_SDK_SESSION_POOL.close)


def _run_sdk(
    *,
    prompt: str,
    model: str,
    timeout: float | None = 180.0,
    cwd: str | None = None,
) -> tuple[str, dict[str, Any]]:
    """Run one completion via the keyed Claude Agent SDK session pool (#715, #818).

    Delegates to the process-wide :data:`_SDK_SESSION_POOL`, which keeps one CLI
    connection per ``(model, cwd)`` so distinct-model experts run concurrently without
    reconnect thrash. Returns ``(text, usage)`` in the same shape as :func:`_run_exec`.
    """
    return _SDK_SESSION_POOL.complete(prompt=prompt, model=model, timeout=timeout, cwd=cwd)


def _sdk_stream_event_text(event: dict[str, Any]) -> str:
    """Extract user-visible text from a Claude SDK raw stream event."""
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


def _sdk_stream_event_thinking(event: dict[str, Any]) -> str:
    """Extract provider-internal thinking from a Claude SDK raw stream event."""
    event_type = str(event.get("type") or "")
    if event_type != "content_block_delta":
        return ""
    delta = event.get("delta")
    if not isinstance(delta, dict):
        return ""
    if delta.get("type") == "thinking_delta":
        return str(delta.get("thinking") or "")
    return ""


_DSPY_FIELD_MARKERS = (
    "[[ ## reasoning ## ]]",
    "[[ ## answer ## ]]",
    "[[ ## next_thought ## ]]",
    "[[ ## next_expert ## ]]",
    "[[ ## next_task ## ]]",
    "[[ ## next_tool_name ## ]]",
    "[[ ## next_tool_args ## ]]",
    "[[ ## workflow_state ## ]]",
    "[[ ## completed ## ]]",
)
_DSPY_FIELD_MARKER_PREFIX_MAX = max(len(marker) for marker in _DSPY_FIELD_MARKERS) - 1


def _first_dspy_field_marker_index(text: str) -> int:
    """Return the first DSPy ChatAdapter field marker offset in ``text``."""

    indexes = [idx for marker in _DSPY_FIELD_MARKERS if (idx := text.find(marker)) >= 0]
    return min(indexes) if indexes else -1


def _split_provider_thinking_contract_delta(
    text: str,
    *,
    marker_tail: str,
    contract_started: bool,
) -> tuple[str, str, str, bool]:
    """Split Claude SDK thinking into hidden provider thinking and DSPy text.

    Claude Code SDK can stream the DSPy ChatAdapter contract on
    ``thinking_delta`` before it later emits a bursty ``text_delta`` copy. Once a
    ``[[ ## field ## ]]`` marker appears, that suffix is no longer merely
    provider-internal thinking for CLIO: it is the model's structured contract and
    must enter the normal LiteLLM text stream immediately so field extractors can
    publish visible deltas over time.

    Returns ``(provider_thinking, contract_text, next_tail, next_started)``.
    """

    if not text:
        return "", "", marker_tail, contract_started
    if contract_started:
        return "", text, marker_tail, True

    combined = marker_tail + text
    marker_index = _first_dspy_field_marker_index(combined)
    if marker_index >= 0:
        return combined[:marker_index], combined[marker_index:], "", True

    next_tail = combined[-_DSPY_FIELD_MARKER_PREFIX_MAX:]
    provider_text = combined[: max(0, len(combined) - len(next_tail))]
    return provider_text, "", next_tail, False


def _streaming_chunk(
    *,
    text: str,
    is_finished: bool,
    finish_reason: str | None = None,
    usage_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a LiteLLM-compatible streaming chunk."""
    usage: dict[str, int] | None = None
    if usage_payload is not None:
        prompt_tokens = int(usage_payload.get("input_tokens", 0) or 0)
        prompt_tokens += int(usage_payload.get("cache_creation_input_tokens", 0) or 0)
        prompt_tokens += int(usage_payload.get("cache_read_input_tokens", 0) or 0)
        completion_tokens = int(usage_payload.get("output_tokens", 0) or 0)
        usage = {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        }
    return {
        "text": text,
        "is_finished": is_finished,
        "finish_reason": finish_reason or ("stop" if is_finished else None),
        "index": 0,
        "tool_use": None,
        "usage": usage,
    }


async def _astream_sdk(
    *,
    prompt: str,
    model: str,
    timeout: float | None = 180.0,
    cwd: str | None = None,
    call_index: int = 0,
) -> AsyncIterator[dict[str, Any]]:
    """Stream one Claude Code SDK call as LiteLLM-compatible chunks."""
    try:
        from claude_agent_sdk import (  # noqa: PLC0415
            AssistantMessage,
            ClaudeAgentOptions,
            ClaudeSDKClient,
            ResultMessage,
            StreamEvent,
            TextBlock,
        )
    except ImportError as exc:
        raise ClaudeCodeCLIUnavailableError(
            "claude_code_transport='sdk' requires the claude-agent-sdk package "
            "(install the 'claude-code' extra)."
        ) from exc

    options = ClaudeAgentOptions(
        tools=[],
        model=model,
        max_turns=1,
        allowed_tools=[],
        permission_mode="bypassPermissions",
        setting_sources=[],
        cwd=cwd,
        include_partial_messages=True,
    )
    client = ClaudeSDKClient(options=options)
    emitted_partial = False
    final_text = ""
    final_usage: dict[str, Any] = {}
    final_reason = "stop"
    response_log: list[dict[str, Any]] = []

    async def _run() -> AsyncIterator[dict[str, Any]]:
        nonlocal emitted_partial, final_text, final_usage, final_reason
        provider_thinking_marker_tail = ""
        provider_thinking_contract_started = False
        promoted_contract_text = ""
        emitted_regular_text = ""
        await client.connect()
        start = time.monotonic()
        last = start
        index = 0
        trace.HF_ON and trace.hot(
            "STREAM-SDK",
            "query_start model=%s prompt_chars=%d cwd=%s",
            model,
            len(prompt),
            cwd or "",
        )
        await client.query(prompt, session_id=uuid.uuid4().hex)
        async for msg in client.receive_response():
            now = time.monotonic()
            index += 1
            trace.HF_ON and trace.hot(
                "STREAM-SDK",
                "recv idx=%d dt_ms=%.1f since_start_ms=%.1f type=%s",
                index,
                (now - last) * 1000.0,
                (now - start) * 1000.0,
                type(msg).__name__,
            )
            last = now
            if isinstance(msg, StreamEvent):
                response_log.append({"message_type": "StreamEvent", "event": msg.event})
                text = _sdk_stream_event_text(msg.event)
                thinking = _sdk_stream_event_thinking(msg.event)
                source_channel = (
                    "thinking_delta" if thinking else ("text_delta" if text else "provider_event")
                )
                stream_audit(
                    "provider.raw_event",
                    provider="claude_code_sdk",
                    call_index=call_index,
                    event_index=index,
                    raw_event_type=str(msg.event.get("type") or ""),
                    source_channel=source_channel,
                    text_len=len(text),
                    thinking_len=len(thinking),
                    chunk_len=len(text or thinking),
                    head=(text or thinking)[:120],
                    full_text=(text or thinking)[:12000],
                )
                trace.HF_ON and trace.hot(
                    "STREAM-SDK",
                    "stream_event sdk_type=%s text_len=%d thinking_len=%d head=%r",
                    str(msg.event.get("type") or ""),
                    len(text),
                    len(thinking),
                    (text or thinking)[:80],
                )
                if thinking:
                    (
                        provider_thinking,
                        promoted_text,
                        provider_thinking_marker_tail,
                        provider_thinking_contract_started,
                    ) = _split_provider_thinking_contract_delta(
                        thinking,
                        marker_tail=provider_thinking_marker_tail,
                        contract_started=provider_thinking_contract_started,
                    )
                    try:
                        from clio_agent.runtime.lm_activity import (  # noqa: PLC0415
                            note_lm_provider_thinking_delta,
                        )

                        if provider_thinking:
                            stream_audit(
                                "provider.normalized",
                                provider="claude_code_sdk",
                                call_index=call_index,
                                event_index=index,
                                source_channel="thinking_delta",
                                normalized_event="turn.trace.delta",
                                chunk_len=len(provider_thinking),
                                duplicate_suppressed=False,
                                head=provider_thinking[:120],
                            )
                            note_lm_provider_thinking_delta(
                                provider_thinking, provider="claude_code_sdk"
                            )
                    except Exception:  # noqa: BLE001 - debug stream must not break provider
                        pass
                    if promoted_text:
                        promoted_contract_text += promoted_text
                        emitted_partial = True
                        stream_audit(
                            "provider.normalized",
                            provider="claude_code_sdk",
                            call_index=call_index,
                            event_index=index,
                            source_channel="thinking_delta",
                            normalized_event="contract.content",
                            chunk_len=len(promoted_text),
                            duplicate_suppressed=False,
                            head=promoted_text[:120],
                        )
                        trace.HF_ON and trace.hot(
                            "STREAM-LITELLM",
                            "yield_promoted_thinking_contract len=%d head=%r",
                            len(promoted_text),
                            promoted_text[:80],
                        )
                        yield _streaming_chunk(text=promoted_text, is_finished=False)
                if text:
                    emitted_regular_text += text
                    if promoted_contract_text and promoted_contract_text.startswith(
                        emitted_regular_text
                    ):
                        stream_audit(
                            "provider.normalized",
                            provider="claude_code_sdk",
                            call_index=call_index,
                            event_index=index,
                            source_channel="text_delta",
                            normalized_event="contract.content",
                            chunk_len=len(text),
                            duplicate_suppressed=True,
                            duplicate_reason="text_delta_duplicates_promoted_contract",
                            head=text[:120],
                        )
                        trace.HF_ON and trace.hot(
                            "STREAM-LITELLM",
                            "suppress_duplicate_text_after_promoted len=%d head=%r",
                            len(text),
                            text[:80],
                        )
                        continue
                    emitted_partial = True
                    stream_audit(
                        "provider.normalized",
                        provider="claude_code_sdk",
                        call_index=call_index,
                        event_index=index,
                        source_channel="text_delta",
                        normalized_event="contract.content",
                        chunk_len=len(text),
                        duplicate_suppressed=False,
                        head=text[:120],
                    )
                    trace.HF_ON and trace.hot(
                        "STREAM-LITELLM",
                        "yield_partial len=%d head=%r",
                        len(text),
                        text[:80],
                    )
                    yield _streaming_chunk(text=text, is_finished=False)
            elif isinstance(msg, AssistantMessage):
                if provider_thinking_marker_tail and not provider_thinking_contract_started:
                    try:
                        from clio_agent.runtime.lm_activity import (  # noqa: PLC0415
                            note_lm_provider_thinking_delta,
                        )

                        note_lm_provider_thinking_delta(
                            provider_thinking_marker_tail, provider="claude_code_sdk"
                        )
                    except Exception:  # noqa: BLE001 - debug stream must not break provider
                        pass
                    provider_thinking_marker_tail = ""
                parts = [b.text for b in msg.content if isinstance(b, TextBlock)]
                if parts:
                    final_text = "".join(parts).strip()
                response_log.append(
                    {
                        "message_type": "AssistantMessage",
                        "content": [
                            {
                                "block_type": type(block).__name__,
                                "text": getattr(block, "text", ""),
                            }
                            for block in msg.content
                        ],
                        "usage": getattr(msg, "usage", None),
                        "stop_reason": getattr(msg, "stop_reason", None),
                    }
                )
                trace.HF_ON and trace.hot(
                    "STREAM-SDK",
                    "assistant_message final_len=%d partial_seen=%s",
                    len(final_text),
                    emitted_partial,
                )
                if isinstance(getattr(msg, "usage", None), dict):
                    final_usage = msg.usage or {}
                if getattr(msg, "stop_reason", None):
                    final_reason = str(msg.stop_reason)
            elif isinstance(msg, ResultMessage):
                response_log.append(
                    {
                        "message_type": "ResultMessage",
                        "usage": getattr(msg, "usage", None),
                        "stop_reason": getattr(msg, "stop_reason", None),
                        "result": getattr(msg, "result", None),
                        "is_error": getattr(msg, "is_error", None),
                        "api_error_status": getattr(msg, "api_error_status", None),
                        "subtype": getattr(msg, "subtype", None),
                    }
                )
                if isinstance(getattr(msg, "usage", None), dict):
                    final_usage = msg.usage or {}
                if getattr(msg, "stop_reason", None):
                    final_reason = str(msg.stop_reason)
                if not final_text and getattr(msg, "result", None):
                    final_text = str(msg.result or "").strip()
                if getattr(msg, "is_error", False):
                    raise ClaudeCodeExecError(
                        f"claude agent sdk returned an error for model={model}: "
                        f"{getattr(msg, 'api_error_status', None) or getattr(msg, 'subtype', None)}"
                    )

    try:
        if timeout is None:
            async for chunk in _run():
                yield chunk
        else:
            async with asyncio.timeout(timeout):
                async for chunk in _run():
                    yield chunk
    except TimeoutError as exc:
        raise ClaudeCodeExecError(
            f"claude agent sdk timed out after {timeout}s (model={model})"
        ) from exc
    finally:
        try:
            await client.disconnect()
        except Exception:  # noqa: BLE001 - best-effort teardown
            logger.warning("claude sdk streaming client disconnect failed", exc_info=True)
        trace.HF_ON and trace.hot(
            "CLAUDE-CODE-IO",
            "response call=%d json=%s",
            call_index,
            _trace_json(
                {
                    "call": call_index,
                    "model": model,
                    "transport": "sdk",
                    "messages": response_log,
                    "final_text": final_text,
                    "final_usage": final_usage,
                    "final_reason": final_reason,
                    "emitted_partial": emitted_partial,
                }
            ),
        )

    if emitted_partial:
        yield _streaming_chunk(
            text="",
            is_finished=True,
            finish_reason=final_reason,
            usage_payload=final_usage,
        )
    elif final_text:
        yield _streaming_chunk(
            text=final_text,
            is_finished=True,
            finish_reason=final_reason,
            usage_payload=final_usage,
        )
    else:
        raise ClaudeCodeExecError(f"claude agent sdk returned empty content (model={model})")


def _build_model_response(
    *,
    text: str,
    model: str,
    usage_payload: dict[str, Any] | None = None,
    request_id: str | None = None,
) -> ModelResponse:
    """Wrap a Claude Code result in a LiteLLM ``ModelResponse``."""
    usage_payload = usage_payload or {}
    prompt_tokens = int(usage_payload.get("input_tokens", 0) or 0)
    prompt_tokens += int(usage_payload.get("cache_creation_input_tokens", 0) or 0)
    prompt_tokens += int(usage_payload.get("cache_read_input_tokens", 0) or 0)
    completion_tokens = int(usage_payload.get("output_tokens", 0) or 0)
    return ModelResponse(
        id=request_id or f"claude-code-{uuid.uuid4().hex}",
        choices=[
            Choices(
                index=0,
                message=Message(role="assistant", content=text),
                finish_reason="stop",
            )
        ],
        created=int(time.time()),
        model=f"claude_code/{model}",
        object="chat.completion",
        usage=Usage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
        ),
    )


class ClaudeCodeLLM(CustomLLM):
    """LiteLLM custom handler that routes ``claude_code/<model>`` to Claude Code."""

    def completion(
        self,
        model: str,
        messages: list,
        api_base: str,
        custom_prompt_dict: dict,
        model_response: ModelResponse,
        print_verbose: Any,
        encoding: Any,
        api_key: Any,
        logging_obj: Any,
        optional_params: dict,
        acompletion: Any = None,
        litellm_params: Any = None,
        logger_fn: Any = None,
        headers: dict | None = None,
        timeout: Any = None,
        client: Any = None,
    ) -> ModelResponse:
        call_index = _next_call_index()
        clean_model = model.removeprefix("claude_code/").removeprefix("cc-")
        prompt = _messages_to_claude_prompt(messages)
        params = optional_params or {}
        # Transport travels per-LM in optional_params (carried on the resolved
        # LMProviderConfig, #818); no process-global env fallback so concurrent
        # experts each get their own transport, not a shared ambient one.
        transport = params.get("claude_code_transport") or DEFAULT_TRANSPORT
        if transport not in ("exec", "sdk"):
            raise ClaudeCodeExecError(
                f"unknown claude_code transport {transport!r} (expected 'exec' or 'sdk')"
            )
        timeout_s = float(timeout) if timeout else 180.0
        cwd = params.get("claude_code_cwd", os.getcwd())
        trace.HF_ON and trace.hot(
            "CLAUDE-CODE-IO",
            "request call=%d json=%s",
            call_index,
            _trace_json(
                {
                    "call": call_index,
                    "mode": "completion",
                    "model": clean_model,
                    "transport": transport,
                    "api_base": api_base,
                    "cwd": cwd,
                    "timeout_s": timeout_s,
                    "sdk_options": {
                        "tools": [],
                        "allowed_tools": [],
                        "max_turns": 1,
                        "setting_sources": [],
                    },
                    "messages": messages,
                    "prompt": prompt,
                    "optional_params": params,
                }
            ),
        )
        trace.HF_ON and trace.hot(
            "CLAUDE-CODE-CALL",
            "completion_start call=%d model=%s transport=%s messages=%d prompt_chars=%d timeout_s=%.1f cwd=%s",
            call_index,
            clean_model,
            transport,
            len(messages or []),
            len(prompt),
            timeout_s,
            cwd or "",
        )
        started = time.monotonic()
        if transport == "sdk":
            text, usage = _run_sdk(prompt=prompt, model=clean_model, timeout=timeout_s, cwd=cwd)
        else:
            text, usage = _run_exec(
                prompt=prompt,
                model=clean_model,
                timeout=timeout_s,
                cwd=cwd,
                call_index=call_index,
            )
        trace.HF_ON and trace.hot(
            "CLAUDE-CODE-CALL",
            "completion_end call=%d model=%s transport=%s elapsed_ms=%.1f text_chars=%d",
            call_index,
            clean_model,
            transport,
            (time.monotonic() - started) * 1000.0,
            len(text),
        )
        trace.HF_ON and trace.hot(
            "CLAUDE-CODE-IO",
            "response call=%d json=%s",
            call_index,
            _trace_json(
                {
                    "call": call_index,
                    "model": clean_model,
                    "transport": transport,
                    "text": text,
                    "usage": usage,
                }
            ),
        )
        return _build_model_response(text=text, model=clean_model, usage_payload=usage)

    async def acompletion(
        self,
        model: str,
        messages: list,
        api_base: str,
        custom_prompt_dict: dict,
        model_response: ModelResponse,
        print_verbose: Any,
        encoding: Any,
        api_key: Any,
        logging_obj: Any,
        optional_params: dict,
        acompletion: Any = None,
        litellm_params: Any = None,
        logger_fn: Any = None,
        headers: dict | None = None,
        timeout: Any = None,
        client: Any = None,
    ) -> ModelResponse:
        return self.completion(
            model=model,
            messages=messages,
            api_base=api_base,
            custom_prompt_dict=custom_prompt_dict,
            model_response=model_response,
            print_verbose=print_verbose,
            encoding=encoding,
            api_key=api_key,
            logging_obj=logging_obj,
            optional_params=optional_params,
            acompletion=acompletion,
            litellm_params=litellm_params,
            logger_fn=logger_fn,
            headers=headers,
            timeout=timeout,
            client=client,
        )

    def streaming(
        self,
        model: str,
        messages: list,
        api_base: str,
        custom_prompt_dict: dict,
        model_response: ModelResponse,
        print_verbose: Any,
        encoding: Any,
        api_key: Any,
        logging_obj: Any,
        optional_params: dict,
        acompletion: Any = None,
        litellm_params: Any = None,
        logger_fn: Any = None,
        headers: dict | None = None,
        timeout: Any = None,
        client: Any = None,
    ) -> Iterator[Any]:
        del (
            model,
            messages,
            api_base,
            custom_prompt_dict,
            model_response,
            print_verbose,
            encoding,
            api_key,
            logging_obj,
            optional_params,
            acompletion,
            litellm_params,
            logger_fn,
            headers,
            timeout,
            client,
        )
        raise ClaudeCodeExecError(
            "Claude Code provider does not support live streaming; use non-streaming completion"
        )

    async def astreaming(  # type: ignore[override]  # base annotates a coroutine-returning-iterator; this async generator satisfies litellm's runtime streaming contract
        self,
        model: str,
        messages: list,
        api_base: str,
        custom_prompt_dict: dict,
        model_response: ModelResponse,
        print_verbose: Any,
        encoding: Any,
        api_key: Any,
        logging_obj: Any,
        optional_params: dict,
        acompletion: Any = None,
        litellm_params: Any = None,
        logger_fn: Any = None,
        headers: dict | None = None,
        timeout: Any = None,
        client: Any = None,
    ) -> AsyncIterator[Any]:
        # Claude Code's ``claude -p`` has no token stream — it returns the full
        # response at once. dspy/litellm drive turns through the streaming path,
        # so emit the completed exec result as a single terminal chunk instead of
        # refusing (refusing left this coroutine unawaited and the turn empty).
        from litellm.types.utils import GenericStreamingChunk  # noqa: PLC0415

        call_index = _next_call_index()
        params = optional_params or {}
        # Transport travels per-LM in optional_params (carried on the resolved
        # LMProviderConfig, #818); no process-global env fallback.
        transport = params.get("claude_code_transport") or DEFAULT_TRANSPORT
        if transport not in ("exec", "sdk"):
            raise ClaudeCodeExecError(
                f"unknown claude_code transport {transport!r} (expected 'exec' or 'sdk')"
            )
        clean_model = model.removeprefix("claude_code/").removeprefix("cc-")
        prompt = _messages_to_claude_prompt(messages)
        timeout_s = float(timeout) if timeout else 180.0
        cwd = params.get("claude_code_cwd", os.getcwd())
        trace.HF_ON and trace.hot(
            "CLAUDE-CODE-IO",
            "request call=%d json=%s",
            call_index,
            _trace_json(
                {
                    "call": call_index,
                    "mode": "astreaming",
                    "model": clean_model,
                    "transport": transport,
                    "api_base": api_base,
                    "cwd": cwd,
                    "timeout_s": timeout_s,
                    "sdk_options": {
                        "tools": [],
                        "allowed_tools": [],
                        "max_turns": 1,
                        "setting_sources": [],
                        "include_partial_messages": transport == "sdk",
                    },
                    "messages": messages,
                    "prompt": prompt,
                    "optional_params": params,
                }
            ),
        )
        trace.HF_ON and trace.hot(
            "CLAUDE-CODE-CALL",
            "astreaming_start call=%d model=%s transport=%s messages=%d prompt_chars=%d timeout_s=%.1f cwd=%s",
            call_index,
            clean_model,
            transport,
            len(messages or []),
            len(prompt),
            timeout_s,
            cwd or "",
        )
        started = time.monotonic()

        if transport == "sdk":
            chunk_count = 0
            text_chars = 0
            try:
                async for chunk in _astream_sdk(
                    prompt=prompt,
                    model=clean_model,
                    timeout=timeout_s,
                    cwd=cwd,
                    call_index=call_index,
                ):
                    chunk_count += 1
                    text_chars += len(str(chunk.get("text") or ""))
                    yield chunk
            finally:
                trace.HF_ON and trace.hot(
                    "CLAUDE-CODE-CALL",
                    "astreaming_end call=%d model=%s transport=%s elapsed_ms=%.1f chunks=%d text_chars=%d",
                    call_index,
                    clean_model,
                    transport,
                    (time.monotonic() - started) * 1000.0,
                    chunk_count,
                    text_chars,
                )
            return

        response = await asyncio.to_thread(
            self.completion,
            model=model,
            messages=messages,
            api_base=api_base,
            custom_prompt_dict=custom_prompt_dict,
            model_response=model_response,
            print_verbose=print_verbose,
            encoding=encoding,
            api_key=api_key,
            logging_obj=logging_obj,
            optional_params=optional_params,
            acompletion=acompletion,
            litellm_params=litellm_params,
            logger_fn=logger_fn,
            headers=headers,
            timeout=timeout,
            client=client,
        )
        choice = response.choices[0]
        usage = getattr(response, "usage", None)
        # A completion (non-stream) choice carries ``.message``; guard defensively
        # since the union also admits StreamingChoices (``.delta``) in the stubs.
        message = getattr(choice, "message", None)
        final_chunk: GenericStreamingChunk = {
            "text": getattr(message, "content", "") or "",
            "is_finished": True,
            "finish_reason": choice.finish_reason or "stop",
            "index": 0,
            "tool_use": None,
            "usage": {
                "prompt_tokens": int(getattr(usage, "prompt_tokens", 0) or 0),
                "completion_tokens": int(getattr(usage, "completion_tokens", 0) or 0),
                "total_tokens": int(getattr(usage, "total_tokens", 0) or 0),
            },
        }
        trace.HF_ON and trace.hot(
            "CLAUDE-CODE-CALL",
            "astreaming_end call=%d model=%s transport=%s elapsed_ms=%.1f chunks=1 text_chars=%d",
            call_index,
            clean_model,
            transport,
            (time.monotonic() - started) * 1000.0,
            len(str(final_chunk.get("text") or "")),
        )
        yield final_chunk


# The once-per-process LiteLLM registration guard is the shared CLI-provider
# machinery (identical lifecycle to codex; only the provider key differs).
ensure_registered, _reset_for_tests = register_custom_provider(
    "claude_code", ClaudeCodeLLM
)


__all__ = [
    "ClaudeCodeCLIUnavailableError",
    "ClaudeCodeExecError",
    "ClaudeCodeLLM",
    "ensure_registered",
]
