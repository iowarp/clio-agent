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
from clio_agent.providers.claude_code_audit import (
    active_gact_ids,
    emit_call_started,
    emit_call_usage,
)
from clio_agent.providers.claude_code_bridge import (
    build_model_response as _build_model_response,
)
from clio_agent.providers.claude_code_options import build_sdk_options
from clio_agent.providers.claude_code_sessions import (
    _SDK_SESSION_POOL,
    _STREAM_CLIENT_POOL,
    _per_call_message_source,
    _run_sdk,
    _SdkSession,
    _SdkSessionPool,
    session_reuse_enabled,
    transient_transport_error_message,
    transient_transport_error_types,
)
from clio_agent.providers.claude_code_thinking_split import (
    _split_provider_thinking_contract_delta,
)
from clio_agent.runtime import trace
from clio_agent.runtime.stream_audit import stream_audit

logger = logging.getLogger(__name__)

try:
    from litellm import CustomLLM
    from litellm.types.utils import ModelResponse
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
    session_id, turn_id, trace_id = active_gact_ids()
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


# The SDK transport machinery (the blocking-path pool in ``claude_code_sdk_pool`` and
# the #891 pooled streaming transport in ``claude_code_sessions``) is imported at the
# top and re-exported below for the historical import seams (#775 no-accretion).


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
    thinking: dict[str, Any] | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """Stream one Claude Code SDK call as LiteLLM-compatible chunks.

    With connection reuse ON
    (:func:`~clio_agent.providers.claude_code_sessions.session_reuse_enabled`, the
    default) the call rides the pooled persistent client (#891): the connect is
    paid once per ``(model, cwd, thinking)`` and every call sends its FULL prompt
    under a FRESH ``session_id`` — the provider's content-prefix cache supplies the
    cache_read win; the per-call conversation boundary supplies
    compartmentalisation (no cross-call/cross-expert context bleed). With the kill
    switch off the per-call path runs byte-for-byte as before — a fresh client,
    connect, query, disconnect. The downstream normalization (#877 marker-split,
    #878 suppression) is identical on both paths — only the client lifecycle
    differs.
    """
    try:
        from claude_agent_sdk import (  # noqa: PLC0415
            AssistantMessage,
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

    call_id = uuid.uuid4().hex
    emit_call_started(  # BEFORE connect: SDK spawn cold-start counts in the call, not the gap (#891)
        call_id=call_id,
        call_index=call_index,
        model=model,
        transport="sdk",
        prompt=prompt,  # ALWAYS the full prompt — the fingerprint tracks cache-prefix stability
    )
    if session_reuse_enabled():
        # Pooled connection (connect reused, hosted on the entry's own loop-thread so
        # it survives the per-call asyncio.run() loops). Full prompt, fresh per-call
        # session_id; the entry's query lock serialises the query→receive cycle.
        entry = _STREAM_CLIENT_POOL.entry_for(model=model, cwd=cwd, thinking=thinking)
        source = entry.stream(
            payload=prompt,
            session_id=uuid.uuid4().hex,
            timeout=timeout,
            on_construct=_STREAM_CLIENT_POOL.bump_construct,
        )
    else:
        # Per-call path (kill switch off): a fresh client, a fresh session_id, the
        # full prompt, and a disconnect after the call.
        client = ClaudeSDKClient(
            options=build_sdk_options(model=model, cwd=cwd, stream=True, thinking=thinking)
        )
        source = _per_call_message_source(
            client, prompt=prompt, session_id=uuid.uuid4().hex, timeout=timeout
        )
    emitted_partial = False
    final_text = ""
    final_usage: dict[str, Any] = {}
    final_reason = "stop"
    response_log: list[dict[str, Any]] = []

    async def _process() -> AsyncIterator[dict[str, Any]]:
        nonlocal emitted_partial, final_text, final_usage, final_reason
        provider_thinking_marker_tail = ""
        provider_thinking_contract_started = False
        promoted_contract_text = ""
        emitted_regular_text = ""
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
        async for msg in source:
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
                    except Exception:  # noqa: BLE001,S110 - debug stream must not break provider
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
                    except Exception:  # noqa: BLE001,S110 - debug stream must not break provider
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

    # Both message sources are timeout-bounded internally (the pooled entry enforces
    # it on its owner loop; the per-call source wraps its own query→receive), so the
    # timeout surfaces here as a TimeoutError to translate.
    try:
        async for chunk in _process():
            yield chunk
    except TimeoutError as exc:
        raise ClaudeCodeExecError(
            f"claude agent sdk timed out after {timeout}s (model={model})"
        ) from exc
    except transient_transport_error_types() as exc:
        # The pooled CLI subprocess died mid-stream (exit 1, no is_error result).
        # The entry already dropped the poisoned client on the abnormal end, so
        # surface a TYPED, audited, transient error → the LM retry layer re-issues
        # on a fresh connection instead of failing the turn (#891 live-crash fix).
        raise ClaudeCodeExecError(
            transient_transport_error_message(model, exc, call_index=call_index)
        ) from exc
    finally:
        emit_call_usage(  # BEFORE disconnect: record call end even if teardown raises (#891)
            call_id=call_id,
            call_index=call_index,
            model=model,
            transport="sdk",
            usage=final_usage,
            output_chars=len(final_text),
        )
        # The pooled client's lifecycle (connect reuse + reset-on-abnormal-end) is
        # owned by the entry; the per-call client disconnects inside its own source.
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
        # Provider-generic thinking (#895): resolved SDK config, or None = default.
        thinking = params.get("claude_code_thinking")
        trace.HF_ON and trace.hot(
            "CLAUDE-CODE-IO",
            "request call=%d json=%s",
            call_index,
            _trace_json(
                {
                    "call": call_index,
                    "mode": "completion",
                    "thinking": thinking,
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
        call_id = uuid.uuid4().hex
        emit_call_started(
            call_id=call_id,
            call_index=call_index,
            model=clean_model,
            transport=transport,
            prompt=prompt,
        )
        started = time.monotonic()
        if transport == "sdk":
            text, usage = _run_sdk(
                prompt=prompt,
                model=clean_model,
                timeout=timeout_s,
                cwd=cwd,
                thinking=thinking,
            )
        else:
            text, usage = _run_exec(
                prompt=prompt,
                model=clean_model,
                timeout=timeout_s,
                cwd=cwd,
                call_index=call_index,
            )
        emit_call_usage(
            call_id=call_id,
            call_index=call_index,
            model=clean_model,
            transport=transport,
            usage=usage,
            output_chars=len(text),
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
        del (model, messages, api_base, custom_prompt_dict, model_response, print_verbose)
        del (encoding, api_key, logging_obj, optional_params, acompletion, litellm_params)
        del (logger_fn, headers, timeout, client)
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
        # Provider-generic thinking (#895): resolved SDK thinking config, or None.
        thinking = params.get("claude_code_thinking")
        trace.HF_ON and trace.hot(
            "CLAUDE-CODE-IO",
            "request call=%d json=%s",
            call_index,
            _trace_json(
                {
                    "call": call_index,
                    "mode": "astreaming",
                    "thinking": thinking,
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
                    thinking=thinking,
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
ensure_registered, _reset_for_tests = register_custom_provider("claude_code", ClaudeCodeLLM)


__all__ = [
    "ClaudeCodeCLIUnavailableError",
    "ClaudeCodeExecError",
    "ClaudeCodeLLM",
    "ensure_registered",
    # Re-exported from providers.claude_code_sessions for the historical import
    # seams (tests + the completion path) — the SDK-session machinery's owner
    # module moved out of this file (#891, #775 no-accretion).
    "_run_sdk",
    "_SDK_SESSION_POOL",
    "_SdkSession",
    "_SdkSessionPool",
    "_STREAM_CLIENT_POOL",
]
