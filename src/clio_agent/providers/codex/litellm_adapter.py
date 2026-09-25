"""LiteLLM ``CustomLLM`` adapter for the direct Codex provider.

Replaces ``providers/codex_litellm.py``'s ``CodexLLM`` entirely -- no flag, no
parallel path. Routes ``dspy.LM(model=f"{LITELLM_PROVIDER}/<model>", ...)``
through the WebSocket transport by default (falling back to SSE per A.6),
builds the Responses-API request from OpenAI-shape messages/tools
(:mod:`clio_agent.providers.codex.responses`), and aggregates the resulting
:mod:`clio_agent.providers.codex.stream_events` into LiteLLM's shapes.

Registered (and routed) under ``constants.LITELLM_PROVIDER`` --
``"codex_direct"`` -- kept deliberately distinct from the catalog/config-facing
``constants.PROVIDER_ID`` ("codex"). This module was ORIGINALLY registered
under "chatgpt" (its catalog id at the time), and litellm ships its own
native "chatgpt" provider whose device-code OAuth client silently
intercepted every turn before this module's handler ever ran. See
:data:`clio_agent.providers.codex.constants.LITELLM_PROVIDER`.

Cancellation registers an abort handle with the SAME transport-agnostic
registry Claude Code and the deleted Codex provider used
(:mod:`clio_agent.providers.claude_code_cancel`), so a cancelled child turn's
in-flight request/WebSocket is torn down the same way every other provider's is.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any, cast

from clio_agent.providers._cli_provider import register_custom_provider
from clio_agent.providers.claude_code_cancel import register_sdk_stream, unregister_sdk_stream
from clio_agent.providers.codex.constants import LITELLM_PROVIDER
from clio_agent.providers.codex.credentials import CodexCredentialStore
from clio_agent.providers.codex.errors import CodexAuthError, CodexError
from clio_agent.providers.codex.responses import (
    build_request_body,
    chat_messages_to_responses_input,
    chat_tools_to_responses_tools,
    extract_reasoning_items,
    with_reasoning_items,
)
from clio_agent.providers.codex.sessions import get_session
from clio_agent.providers.codex.stream_events import (
    Completed,
    ReasoningDelta,
    StreamEvent,
    TextDelta,
    ToolCallDone,
)
from clio_agent.providers.codex.transport_sse import stream_sse_turn
from clio_agent.providers.codex.transport_ws import (
    WsPreStreamFailure,
    drop_connection_after_cancel,
    stream_ws_turn,
)

logger = logging.getLogger(__name__)

try:
    from litellm import CustomLLM
    from litellm.types.utils import (
        ChatCompletionUsageBlock,
        Choices,
        GenericStreamingChunk,
        Message,
        ModelResponse,
        Usage,
    )
except ImportError as e:  # pragma: no cover - litellm is a hard dep
    raise ImportError("litellm must be installed to use the Codex provider") from e

#: Lazily constructed so importing this module never touches the disk-backed
#: credential store (tests / installs that don't use this provider).
_store: CodexCredentialStore | None = None


def _credential_store() -> CodexCredentialStore:
    global _store  # noqa: PLW0603
    if _store is None:
        _store = CodexCredentialStore()
    return _store


def _reset_store_for_tests(store: CodexCredentialStore | None = None) -> None:
    global _store  # noqa: PLW0603
    _store = store


def _session_id() -> str:
    from clio_agent.gact.context import (
        active_session_id,  # noqa: PLC0415 - avoid a boot import cycle
    )

    return active_session_id() or f"codex-{uuid.uuid4().hex}"


def _bare_model(model: str) -> str:
    return model.removeprefix(f"{LITELLM_PROVIDER}/").removeprefix("cg-")


async def stream_turn(
    *, model: str, messages: list[dict[str, Any]], params: dict[str, Any]
) -> AsyncIterator[StreamEvent]:
    """Resolve auth, build the request, pick a transport, and yield normalized events.

    A 401 refreshes the credential once and retries the whole turn (A.7); a
    pre-stream WebSocket failure falls back to SSE for the rest of this turn,
    per :func:`clio_agent.providers.codex.transport_ws.stream_ws_turn`.
    """

    session_id = _session_id()
    state = get_session(session_id)
    instructions, chat_input = chat_messages_to_responses_input(messages)
    input_items = with_reasoning_items(chat_input, state.reasoning_items)
    tools = chat_tools_to_responses_tools(params.get("tools"))
    body = build_request_body(
        model=_bare_model(model),
        input_items=input_items,
        instructions=instructions,
        tools=tools,
        tool_choice=params.get("tool_choice"),
        session_id=session_id,
        reasoning_effort=params.get("codex_reasoning_effort"),
    )
    forced_sse = str(params.get("codex_transport") or "").strip().lower() == "sse"
    use_ws = not forced_sse and not state.sse_only

    store = _credential_store()
    credential = store.get_valid_credential()
    refreshed_once = False
    abort_handle = register_sdk_stream(session_id, abort=lambda: None)
    try:
        while True:
            try:
                events = (
                    stream_ws_turn(
                        credential=credential, body=body, session_id=session_id, state=state
                    )
                    if use_ws
                    else stream_sse_turn(credential=credential, body=body, session_id=session_id)
                )
                async for event in events:
                    if isinstance(event, Completed):
                        state.reasoning_items = extract_reasoning_items(event.output_items)
                    yield event
                return
            except WsPreStreamFailure:
                use_ws = False
                continue
            except CodexAuthError:
                if refreshed_once:
                    raise
                refreshed_once = True
                credential = store.get_valid_credential(force_refresh=True)
                continue
    except BaseException:
        if use_ws:
            await drop_connection_after_cancel(state)
        raise
    finally:
        unregister_sdk_stream(abort_handle)


def _usage_from_responses(usage: dict[str, Any]) -> dict[str, int]:
    """Map the Responses API's ``usage`` block onto LiteLLM's field names."""

    input_tokens = int(usage.get("input_tokens", 0) or 0)
    output_tokens = int(usage.get("output_tokens", 0) or 0)
    reasoning_tokens = int(
        (usage.get("output_tokens_details") or {}).get("reasoning_tokens", 0) or 0
    )
    total = int(usage.get("total_tokens", 0) or 0) or (input_tokens + output_tokens)
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "reasoning_output_tokens": reasoning_tokens,
        "total_tokens": total,
    }


def _build_model_response(
    *, text: str, model: str, tool_calls: list[dict[str, Any]], usage_payload: dict[str, Any]
) -> ModelResponse:
    """Wrap an aggregated Codex turn in a LiteLLM ``ModelResponse``."""

    message = Message(role="assistant", content=text or None)
    if tool_calls:
        message.tool_calls = tool_calls  # type: ignore[assignment]  # litellm accepts a dict at runtime; its stub wants typed tool-call objects
    prompt_tokens = usage_payload.get("input_tokens", 0)
    completion_tokens = usage_payload.get("output_tokens", 0)
    reasoning_tokens = usage_payload.get("reasoning_output_tokens", 0)
    total = usage_payload.get("total_tokens", 0) or (prompt_tokens + completion_tokens)
    return ModelResponse(
        id=f"codex-{uuid.uuid4().hex}",
        choices=[
            Choices(index=0, message=message, finish_reason="tool_calls" if tool_calls else "stop")
        ],
        created=int(time.time()),
        model=f"{LITELLM_PROVIDER}/{model}",
        object="chat.completion",
        usage=Usage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total,
            completion_tokens_details={"reasoning_tokens": reasoning_tokens},
        ),
    )


def _tool_call_dict(call: ToolCallDone) -> dict[str, Any]:
    return {
        "id": call.call_id,
        "type": "function",
        "function": {"name": call.name, "arguments": call.arguments},
        "index": call.index,
    }


async def _run_turn_collecting(model: str, messages: list, params: dict[str, Any]) -> ModelResponse:
    text_parts: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    usage: dict[str, int] = {}
    async for event in stream_turn(model=model, messages=messages, params=params):
        if isinstance(event, TextDelta):
            text_parts.append(event.text)
        elif isinstance(event, ToolCallDone):
            tool_calls.append(_tool_call_dict(event))
        elif isinstance(event, Completed):
            usage = _usage_from_responses(event.usage)
    return _build_model_response(
        text="".join(text_parts),
        model=_bare_model(model),
        tool_calls=tool_calls,
        usage_payload=usage,
    )


def _to_streaming_chunk(event: StreamEvent, *, index: int) -> GenericStreamingChunk | None:
    if isinstance(event, TextDelta):
        return GenericStreamingChunk(
            text=event.text,
            tool_use=None,
            is_finished=False,
            finish_reason="",
            usage=None,
            index=index,
        )
    if isinstance(event, ReasoningDelta):
        return GenericStreamingChunk(
            text="",
            tool_use=None,
            is_finished=False,
            finish_reason="",
            usage=None,
            index=index,
            provider_specific_fields={"reasoning_delta": event.text},
        )
    if isinstance(event, ToolCallDone):
        return GenericStreamingChunk(
            text="",
            tool_use=cast(Any, _tool_call_dict(event)),
            is_finished=False,
            finish_reason="",
            usage=None,
            index=index,
        )
    if isinstance(event, Completed):
        usage = _usage_from_responses(event.usage)
        return GenericStreamingChunk(
            text="",
            tool_use=None,
            is_finished=True,
            finish_reason="stop",
            usage=cast(
                ChatCompletionUsageBlock,
                {
                    "prompt_tokens": usage["input_tokens"],
                    "completion_tokens": usage["output_tokens"],
                    "total_tokens": usage["total_tokens"],
                },
            ),
            index=index,
        )
    return None


class CodexLLM(CustomLLM):
    """LiteLLM custom handler routing ``"codex_direct/<model>"`` to the Codex backend directly."""

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
        import asyncio  # noqa: PLC0415

        return asyncio.run(_run_turn_collecting(model, messages, optional_params or {}))

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
        return await _run_turn_collecting(model, messages, optional_params or {})

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
    ) -> Iterator[GenericStreamingChunk]:
        # Sync streaming is a compatibility fallback (DSPy drives turns through
        # astreaming); drain the whole async stream and yield one terminal chunk,
        # matching the pattern the deleted CodexLLM used.
        import asyncio  # noqa: PLC0415

        response = asyncio.run(_run_turn_collecting(model, messages, optional_params or {}))
        message = response.choices[0].message  # type: ignore[union-attr]
        usage = response.usage  # type: ignore[attr-defined]  # ModelResponse carries usage at runtime; the stub omits it
        yield GenericStreamingChunk(
            text=str(message.content or ""),
            tool_use=cast(Any, message.tool_calls[0]) if message.tool_calls else None,  # type: ignore[union-attr]
            is_finished=True,
            finish_reason=response.choices[0].finish_reason or "stop",  # type: ignore[union-attr]
            usage=cast(
                ChatCompletionUsageBlock,
                {
                    "prompt_tokens": usage.prompt_tokens if usage else 0,
                    "completion_tokens": usage.completion_tokens if usage else 0,
                    "total_tokens": usage.total_tokens if usage else 0,
                },
            ),
            index=0,
        )

    async def astreaming(  # type: ignore[override, misc]  # base annotates a coroutine-returning-iterator; this async generator satisfies litellm's runtime streaming contract
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
    ) -> AsyncIterator[GenericStreamingChunk]:
        try:
            async for event in stream_turn(
                model=model, messages=messages, params=optional_params or {}
            ):
                chunk = _to_streaming_chunk(event, index=0)
                if chunk is not None:
                    yield chunk
        except CodexError as exc:
            logger.warning(
                "codex turn failed reason=%s: %s", getattr(exc, "reason", "codex_error"), exc
            )
            raise


ensure_registered, _reset_registration_for_tests = register_custom_provider(
    LITELLM_PROVIDER, CodexLLM
)


__all__ = [
    "CodexLLM",
    "ensure_registered",
    "stream_turn",
]
