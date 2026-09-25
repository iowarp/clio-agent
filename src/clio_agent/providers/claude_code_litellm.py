"""LiteLLM ``CustomLLM`` provider for Claude Code.

Routes ``dspy.LM(model="claude_code/<model>", ...)`` calls through the Claude
Agent SDK's ONE per-GACT-session pooled client (S2 B1, :mod:`claude_code_sessions`)
so CLIO can use the user's Claude Code subscription auth without bypassing the
DSPy/LiteLLM provider contract. Built-in tools are disabled; CLIO's own
planner + MCP/tool gateway are the only tool execution layer (B3/B7 — Claude
Code stays a bare model engine, never an inner loop of its own).
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any

from clio_agent.providers._cli_provider import (
    messages_to_prompt,
    raise_model_rejected,
    register_custom_provider,
)
from clio_agent.providers.claude_code_audit import (
    emit_call_started,
    emit_call_usage,
    emit_request_trace,
)
from clio_agent.providers.claude_code_audit import trace_json as _trace_json
from clio_agent.providers.claude_code_blocking import _run_sdk
from clio_agent.providers.claude_code_bridge import (
    build_model_response as _build_model_response,
)
from clio_agent.providers.claude_code_multimodal import (
    messages_to_claude_input,
    native_input_summary,
    redact_message_attachments,
)
from clio_agent.providers.claude_code_plan_limit import (
    plan_limit_from_rate_limit_event,
    plan_limit_from_result,
)
from clio_agent.providers.claude_code_sessions import (
    _STREAM_CLIENT_POOL,
    _active_gact_session_id,
    _streaming_chunk,
    transient_transport_error_message,
    transient_transport_error_types,
)
from clio_agent.providers.claude_code_stateful import (
    StatefulSend,
    resolve_stateful_send,
)
from clio_agent.providers.claude_code_stream_events import (
    stream_event_text as _sdk_stream_event_text,
)
from clio_agent.providers.claude_code_stream_events import (
    stream_event_thinking as _sdk_stream_event_thinking,
)
from clio_agent.providers.claude_code_system_prompt import (
    prepare_claude_request,
    split_system_prompt,
)
from clio_agent.providers.claude_code_thinking_split import (
    _split_provider_thinking_contract_delta,
    emit_provider_thinking,
    note_redacted_thinking,
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
# The Claude Agent SDK transport (persistent pooled CLI session) is the only
# transport since v0.8.0; "exec" (one `claude -p` per call) was deleted.
DEFAULT_TRANSPORT = "sdk"

#: A ``ResultMessage.api_error_status`` of 404 is the ONLY definitive
#: model-rejection signal claude_code exposes (#1184, #1211 review A3/D3). Any
#: other ``is_error`` status (429/5xx/None) stays on the existing generic
#: error path -- transient noise must never be misclassified as a rejection.
#: Model discovery no longer probes per-model status; it trusts the maintained
#: catalog instead (see ``providers.model_discovery.claude_code_catalog``).
CLAUDE_CODE_REJECTION_STATUS = 404


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


def _messages_to_claude_input(
    messages: list[dict[str, Any]],
) -> tuple[str, list[dict[str, Any]]]:
    """Return role-hardened text plus native Agent SDK image/PDF blocks."""

    return messages_to_claude_input(
        messages,
        serialize_text=_messages_to_claude_prompt,
        unsupported_multimodal_exc=ClaudeCodeUnsupportedMultimodalError,
    )


def _prepare_claude_request(
    messages: list[dict[str, Any]],
) -> tuple[str, str, list[dict[str, Any]]]:
    """B4: split CLIO's system message out, then serialize the rest — this
    provider's own exception type bound onto the shared helper."""
    return prepare_claude_request(
        messages,
        serialize_text=_messages_to_claude_prompt,
        unsupported_multimodal_exc=ClaudeCodeUnsupportedMultimodalError,
    )


def _split_for_native_blocks(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The non-system remainder of ``messages`` (a delta tail's native-block
    extraction excludes the system row exactly like the full-prompt path)."""
    return split_system_prompt(
        messages, unsupported_multimodal_exc=ClaudeCodeUnsupportedMultimodalError
    )[1]


async def _astream_sdk(
    *,
    prompt: str,
    model: str,
    timeout: float | None = 180.0,
    cwd: str | None = None,
    call_index: int = 0,
    thinking: dict[str, Any] | None = None,
    system_prompt: str | None = None,
    send: StatefulSend | None = None,
    native_blocks: list[dict[str, Any]] | None = None,
    usage_sink: dict[str, Any] | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """Stream one Claude Code SDK call as LiteLLM-compatible chunks.

    S2 (B1): rides the ONE pooled client for the active GACT session, reused
    across every turn. ``system_prompt`` (B4) rides
    ``ClaudeAgentOptions.system_prompt``, never the query text. ``usage_sink``,
    when given, collects the raw SDK usage dict for the blocking path.
    """
    # A structured mcp-2 unavailability error, not a raw ImportError trace.
    from clio_agent.providers.claude_code_options import require_claude_agent_sdk  # noqa: PLC0415

    await asyncio.to_thread(require_claude_agent_sdk)
    import claude_agent_sdk as _sdk  # noqa: PLC0415

    AssistantMessage, ResultMessage = _sdk.AssistantMessage, _sdk.ResultMessage
    StreamEvent, TextBlock = _sdk.StreamEvent, _sdk.TextBlock
    # B17: only the real SDK (or a fake that opts in) exposes RateLimitEvent; a
    # minimal test fake without it simply never matches the isinstance below.
    RateLimitEvent = getattr(_sdk, "RateLimitEvent", None)

    call_id = (
        send.call_id if send is not None else ""
    ) or uuid.uuid4().hex  # #901: join stateful↔TTFT on ONE id
    emit_call_started(  # BEFORE connect: SDK spawn cold-start counts in the call, not the gap (#891)
        call_id=call_id,
        call_index=call_index,
        model=model,
        transport="sdk",
        prompt=prompt,  # ALWAYS the full prompt — the fingerprint tracks cache-prefix stability
    )
    # Full-vs-delta send plan (#901). ``send`` carries the actual payload (delta tail
    # or full prompt) + the session_id to send under (stable across a delta run, fresh
    # on any reset). ``None`` / not-engaged ⇒ the full prompt under a fresh id — the
    # byte-identical pre-#901 transport.
    payload = send.payload if send is not None else prompt
    session_id = send.session_id if send is not None else uuid.uuid4().hex
    gact_sid_for_pool = _active_gact_session_id()
    entry = _STREAM_CLIENT_POOL.entry_for(
        session_id=gact_sid_for_pool,
        gact_session_id=gact_sid_for_pool,
        thinking=thinking,
        system_prompt=system_prompt,
    )
    source = entry.stream(
        payload=payload,
        native_blocks=list(native_blocks or []),
        session_id=session_id,
        timeout=timeout,
        on_construct=_STREAM_CLIENT_POOL.bump_construct,
        model=model,
        cwd=cwd,
        thinking=thinking,
        system_prompt=system_prompt,
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
        redacted_thinking_total = 0
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
                    emit_provider_thinking(
                        provider_thinking, call_index=call_index, event_index=index
                    )
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
                else:
                    # No thinking TEXT on this event. If it is a redacted thinking
                    # delta (empty text + estimated_tokens — CLI thinking display
                    # 'omitted'), record the typed provider_thinking_redacted
                    # reason instead of letting the CoT vanish silently.
                    redacted_thinking_total = note_redacted_thinking(
                        msg.event,
                        call_index=call_index,
                        event_index=index,
                        total=redacted_thinking_total,
                    )
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
                    emit_provider_thinking(
                        provider_thinking_marker_tail, call_index=call_index, event_index=index
                    )
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
                    status = getattr(msg, "api_error_status", None)
                    if status == CLAUDE_CODE_REJECTION_STATUS:
                        # #1184 / #1211 review A3: a definitive rejection (verified
                        # live: api_error_status 404 + a "issue with the selected
                        # model" result text) -- never retried as transient, and the
                        # CLI's own explanatory text rides into the transcript
                        # (see raise_model_rejected's docstring), not just the
                        # bare status integer the old raise kept.
                        raise_model_rejected(
                            message=(
                                f"claude_code rejected model {model!r} "
                                f"(api_error_status={status}): "
                                f"{getattr(msg, 'result', '') or 'model not available'}"
                            ),
                            model=f"claude_code/{model}",
                            llm_provider="claude_code",
                        )
                    # B17: a structured 429 is a typed, terminal plan-limit error
                    # (never the generic, retry-tempting ClaudeCodeExecError).
                    plan_limit = plan_limit_from_result(msg)
                    if plan_limit is not None:
                        raise plan_limit
                    raise ClaudeCodeExecError(
                        f"claude agent sdk returned an error for model={model}: "
                        f"{status or getattr(msg, 'subtype', None)}"
                    )
            elif RateLimitEvent is not None and isinstance(msg, RateLimitEvent):
                # B17: a "rejected" status is the SAME typed plan-limit error as a
                # 429 result, raised as soon as the CLI reports it.
                response_log.append(
                    {
                        "message_type": "RateLimitEvent",
                        "info": getattr(msg, "rate_limit_info", None),
                    }
                )
                plan_limit = plan_limit_from_rate_limit_event(msg)
                if plan_limit is not None:
                    raise plan_limit

    # The pooled entry's query→receive cycle is timeout-bounded internally on its
    # owner loop, so a timeout surfaces here as a TimeoutError to translate.
    try:
        async for chunk in _process():
            yield chunk
    except TimeoutError as exc:
        if send is not None:
            send.note_error()  # drop the poisoned session → next call resets (provider_error)
        raise ClaudeCodeExecError(
            f"claude agent sdk timed out after {timeout}s (model={model})"
        ) from exc
    except transient_transport_error_types() as exc:
        # The pooled CLI subprocess died mid-stream; surface a TYPED, audited,
        # transient error (carrying its stderr tail, B17) so the LM retry
        # layer re-issues on a fresh connection instead of failing the turn.
        if send is not None:
            send.note_error()  # drop the poisoned stateful session → bounded full resend
        raise ClaudeCodeExecError(
            transient_transport_error_message(
                model, exc, call_index=call_index, stderr_tail=entry.stderr_ring.tail()
            )
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
        if usage_sink is not None:
            usage_sink.update(final_usage)
        # The pooled client's lifecycle (connect reuse + reset-on-abnormal-end) is
        # owned by the entry, keyed to the active GACT session (B1) — this
        # generator never connects or disconnects a client itself.
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


#: The SDK options each entry point reports in its request trace. CLIO owns tool
#: execution, so both disable Claude's built-in tools; only the streaming path
#: asks for partial messages.
_BLOCKING_SDK_OPTIONS: dict[str, Any] = {
    "tools": [],
    "allowed_tools": [],
    "max_turns": 1,
    "setting_sources": [],
}
_STREAMING_SDK_OPTIONS: dict[str, Any] = {**_BLOCKING_SDK_OPTIONS, "include_partial_messages": True}


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
        system_prompt, prompt, native_blocks = _prepare_claude_request(messages)
        params = optional_params or {}
        # Transport travels per-LM in optional_params (carried on the resolved
        # LMProviderConfig, #818); no process-global env fallback so concurrent
        # experts each get their own transport, not a shared ambient one.
        transport = params.get("claude_code_transport") or DEFAULT_TRANSPORT
        if transport != "sdk":
            raise ClaudeCodeExecError(
                f"claude_code transport {transport!r} was removed in the v0.8.0 cleanup — "
                "sdk (pooled Claude Agent SDK session) is the only transport; unset "
                "CLIO_CLAUDE_CODE_TRANSPORT / lm.claude_code_transport"
            )
        timeout_s = float(timeout) if timeout else 180.0
        cwd = params.get("claude_code_cwd", os.getcwd())
        # Provider-generic thinking (#895): resolved SDK config, or None = default.
        thinking = params.get("claude_code_thinking")
        emit_request_trace(
            call_index=call_index,
            mode="completion",
            model=clean_model,
            transport=transport,
            api_base=api_base,
            cwd=cwd,
            timeout_s=timeout_s,
            thinking=thinking,
            sdk_options=_BLOCKING_SDK_OPTIONS,
            # The request SHAPE, with attachment bytes elided and the redaction
            # typed. Deleting this record outright (rather than redacting it)
            # removed the only view of how many parts a multimodal request
            # carried, in what order, of what media type.
            messages=redact_message_attachments(messages),
            prompt=prompt,
            native_inputs=native_input_summary(native_blocks),
            optional_params=params,
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
        # ``_run_sdk`` rides the same per-session pooled client ``_astream_sdk``
        # uses for streaming (S2 B1) and emits its own call_started/call_usage
        # audit rows internally — this method no longer duplicates them.
        text, usage = _run_sdk(
            prompt=prompt,
            native_blocks=native_blocks,
            model=clean_model,
            timeout=timeout_s,
            cwd=cwd,
            thinking=thinking,
            system_prompt=system_prompt,
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
        # The pooled SDK bridge BLOCKS its caller; awaited on the server loop (the goal
        # judge, #1333) it froze the loop, so it runs on a worker (contextvars copied).
        return await asyncio.to_thread(
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
        call_index = _next_call_index()
        params = optional_params or {}
        # Transport travels per-LM in optional_params (carried on the resolved
        # LMProviderConfig, #818); no process-global env fallback.
        transport = params.get("claude_code_transport") or DEFAULT_TRANSPORT
        if transport != "sdk":
            raise ClaudeCodeExecError(
                f"claude_code transport {transport!r} was removed in the v0.8.0 cleanup — "
                "sdk (pooled Claude Agent SDK session) is the only transport; unset "
                "CLIO_CLAUDE_CODE_TRANSPORT / lm.claude_code_transport"
            )
        clean_model = model.removeprefix("claude_code/").removeprefix("cc-")
        system_prompt, prompt, native_blocks = _prepare_claude_request(messages)
        timeout_s = float(timeout) if timeout else 180.0
        cwd = params.get("claude_code_cwd", os.getcwd())
        # Provider-generic thinking (#895): resolved SDK thinking config, or None.
        thinking = params.get("claude_code_thinking")
        emit_request_trace(
            call_index=call_index,
            mode="astreaming",
            model=clean_model,
            transport=transport,
            api_base=api_base,
            cwd=cwd,
            timeout_s=timeout_s,
            thinking=thinking,
            sdk_options=_STREAMING_SDK_OPTIONS,
            messages=redact_message_attachments(messages),
            prompt=prompt,
            native_inputs=native_input_summary(native_blocks),
            optional_params=params,
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

        chunk_count = 0
        text_chars = 0
        # Full-vs-stateful-delta send plan (#901). Inert (fresh id, full prompt)
        # unless a ReActV2 scope token is active.
        send = resolve_stateful_send(
            messages=list(messages or []),
            full_prompt=prompt,
            model=clean_model,
            cwd=cwd,
            thinking=thinking,
            # B4: a delta tail's own serialization must also exclude the system
            # row (in practice a delta tail never carries the first message
            # anyway, but this keeps the guarantee structural, not incidental).
            serialize=lambda rows: _messages_to_claude_input(_split_for_native_blocks(rows))[0],
            call_index=call_index,
        )
        if send.message_batch:
            _, native_blocks = _messages_to_claude_input(
                _split_for_native_blocks(send.message_batch)
            )
        try:
            async for chunk in _astream_sdk(
                prompt=prompt,
                model=clean_model,
                timeout=timeout_s,
                cwd=cwd,
                call_index=call_index,
                thinking=thinking,
                system_prompt=system_prompt,
                send=send,
                native_blocks=native_blocks,
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


# The once-per-process LiteLLM registration guard is the shared CLI-provider
# machinery (identical lifecycle to codex; only the provider key differs).
ensure_registered, _reset_for_tests = register_custom_provider("claude_code", ClaudeCodeLLM)


__all__ = [
    "ClaudeCodeCLIUnavailableError",
    "ClaudeCodeExecError",
    "ClaudeCodeLLM",
    "ensure_registered",
    "_messages_to_claude_input",
    "_run_sdk",
    # Re-exported from providers.claude_code_sessions for the historical import
    # seam (tests) — the ONE per-GACT-session pooled client (S2 B1; #775
    # no-accretion keeps its owner module separate from this one).
    "_STREAM_CLIENT_POOL",
]
