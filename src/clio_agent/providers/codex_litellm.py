"""LiteLLM ``CustomLLM`` provider for the official OpenAI Codex Python SDK.

Routes ``dspy.LM(model="codex/<model>", ...)`` through :mod:`openai_codex`
so the user's existing ChatGPT/Codex authentication is reused without CLIO
owning a shell, CLI, or app-server protocol transport.

Design notes
------------

- **No agentic loop.** We drive Codex with a read-only sandbox so its
  built-in shell/filesystem tools are inert; each turn produces only an
  answer. Clio's planner does the real orchestration.

- **One transport.** ``transport="sdk"`` imports the published Python SDK.
  ``exec`` and CLIO-owned app-server transports do not exist and never serve as
  fallbacks.

- **Auth lives in the SDK runtime.** CLIO never receives the user's ChatGPT
  cookie or OpenAI key; the SDK reuses the existing Codex authentication.

- **Streaming.** The SDK's typed turn stream carries real assistant deltas,
  provider reasoning summaries, optional raw reasoning deltas, and usage.
  ``streaming()`` / ``astreaming()`` MUST return a real
  (async) iterator (a bare coroutine produced the #708 mid-stream crash).

- **Registration is lazy + idempotent.** ``ensure_registered()`` is
  called from ``config.create_lm()`` / ``create_planner_lm()`` only when
  ``config.provider == "codex"`` — keeps Codex out of the import graph
  for tests / installs that don't use it.
"""

from __future__ import annotations

import logging
import tempfile
import time
import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any, cast

from openai_codex.types import ReasoningEffort

from clio_agent.providers._cli_provider import (
    messages_to_prompt,
    register_custom_provider,
)
from clio_agent.providers.codex_stream import (
    DEFAULT_TURN_TIMEOUT_S,
    CodexSDKError,
    _next_call_index,
    astream_sdk,
    run_sdk,
    usage_chunk,
)
from clio_agent.providers.native_attachment_bounds import (
    NativeAttachmentTooLargeError,
    base64_byte_length,
    check_block_bytes,
    check_total_bytes,
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


#: The sole transport: the published official Python SDK.
Transport = str
DEFAULT_TRANSPORT: Transport = "sdk"


def _resolve_codex_cwd(params: dict[str, Any]) -> str:
    """Return the explicit provider cwd or a neutral non-workspace directory.

    A bare LM call must not inherit Clio's repository as Codex workspace context:
    doing so loads ``AGENTS.md``, workspace capabilities, and coding-agent state
    ahead of the actual DSPy prompt.
    """
    configured = params.get("codex_cwd")
    return str(configured) if configured else tempfile.gettempdir()


class CodexUnsupportedMultimodalError(CodexSDKError):
    """Raised when the Codex SDK transport receives content it would drop."""


def _messages_to_codex_prompt(messages: list[dict[str, Any]]) -> str:
    """Serialize OpenAI-shape messages into a hardened Codex prompt.

    Codex takes a single prompt string per turn, so we cannot pass native
    chat messages through. Thin wrapper over the shared prompt
    serializer (:func:`clio_agent.providers._cli_provider.messages_to_prompt`)
    with Codex's own unsupported-multimodal exception + transport label.
    """
    return messages_to_prompt(
        messages,
        unsupported_multimodal_exc=CodexUnsupportedMultimodalError,
        transport_label="Codex",
    )


def _data_url_bytes(value: str) -> int:
    """Decoded byte length of a base64 data URL, or ``0`` for a non-data URL.

    Arithmetic only — an oversized image is refused without ever being decoded.
    A remote URL is not CLIO's payload to size, so it contributes nothing here.
    """

    if not value.startswith("data:"):
        return 0
    _header, separator, payload = value.partition(",")
    if not separator:
        return 0
    return base64_byte_length(payload)


def _check_image_bytes(value: str) -> None:
    """Refuse one oversized Codex image before it is expanded into a request.

    Shares the bounds module with the Claude attach path so the two providers
    cannot drift; the SDK would otherwise accept the value, expand it, and fail
    the round-trip with a raw transport error instead of an explainable refusal.
    """

    try:
        check_block_bytes("image", _data_url_bytes(value))
    except NativeAttachmentTooLargeError as exc:
        raise CodexUnsupportedMultimodalError(str(exc)) from exc


def _check_total_image_bytes(values: list[str]) -> None:
    """Refuse a request whose Codex images sum past the aggregate ceiling."""

    try:
        check_total_bytes(sum(_data_url_bytes(value) for value in values))
    except NativeAttachmentTooLargeError as exc:
        raise CodexUnsupportedMultimodalError(str(exc)) from exc


def _messages_to_codex_input(messages: list[dict[str, Any]]) -> tuple[str, list[str]]:
    """Return the hardened transcript plus native SDK image inputs.

    The Codex SDK accepts data URLs as typed ``ImageInput`` values.  Keep those
    values out of the serialized transcript and audit log while preserving all
    text and role boundaries through the existing hardened serializer.
    """

    text_messages: list[dict[str, Any]] = []
    image_urls: list[str] = []
    for message in messages:
        content = message.get("content")
        if not isinstance(content, list):
            text_messages.append(message)
            continue
        text_parts: list[Any] = []
        for part in content:
            if not isinstance(part, dict):
                text_parts.append(part)
                continue
            part_type = str(part.get("type") or "").strip().lower()
            if part_type not in {"image", "image_url", "input_image"} and "image_url" not in part:
                text_parts.append(part)
                continue
            image_value = part.get("image_url") or part.get("url")
            if isinstance(image_value, dict):
                image_value = image_value.get("url")
            if not isinstance(image_value, str) or not image_value.strip():
                raise CodexUnsupportedMultimodalError(
                    "Codex SDK image message parts require a non-empty URL"
                )
            resolved = image_value.strip()
            _check_image_bytes(resolved)
            image_urls.append(resolved)
        text_messages.append({**message, "content": text_parts})
    _check_total_image_bytes(image_urls)
    return _messages_to_codex_prompt(text_messages), image_urls


def _build_model_response(
    *,
    text: str,
    model: str,
    usage_payload: dict[str, Any] | None = None,
    request_id: str | None = None,
) -> ModelResponse:
    """Wrap a Codex completion in a LiteLLM ``ModelResponse``.

    ``usage_payload`` is the normalized SDK token breakdown; ``None`` stubs
    zeros (cost-tracking callers fall back to the price-table heuristic in
    ``gact/app.py``). Codex's
    ``input_tokens`` already includes the cached subset and ``output_tokens``
    already includes reasoning, so we do NOT re-sum them (that would double-count).
    """
    usage_payload = usage_payload or {}
    prompt_tokens = int(usage_payload.get("input_tokens", 0) or 0)
    completion_tokens = int(usage_payload.get("output_tokens", 0) or 0)
    reasoning_tokens = int(usage_payload.get("reasoning_output_tokens", 0) or 0)
    total = int(usage_payload.get("total_tokens", 0) or 0) or (prompt_tokens + completion_tokens)
    return ModelResponse(
        id=request_id or f"codex-{uuid.uuid4().hex}",
        choices=[
            Choices(
                index=0,
                message=Message(role="assistant", content=text),
                finish_reason="stop",
            )
        ],
        created=int(time.time()),
        model=f"codex/{model}",
        object="chat.completion",
        usage=Usage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total,
            completion_tokens_details={"reasoning_tokens": reasoning_tokens},
        ),
    )


def _resolve_effort(params: dict[str, Any]) -> ReasoningEffort | None:
    """Resolve the codex reasoning effort from the #895 thinking plan.

    ``codex_reasoning_effort`` is set by ``providers.thinking.resolve_thinking``
    (off→``none``, low/medium/high pass through). ``None`` means the knob was
    unset — no effort is pinned and codex uses its own default. This is the fix
    for the silent no-op: a requested level now reaches ``turn/start``.
    """
    effort = params.get("codex_reasoning_effort")
    return ReasoningEffort(str(effort)) if effort else None


class CodexLLM(CustomLLM):
    """LiteLLM custom handler routing ``codex/<model>`` to ``openai_codex``."""

    @staticmethod
    def _resolve_transport(params: dict) -> str:
        """Resolve the sole SDK transport or raise a typed hard error."""
        transport = params.get("codex_transport") or DEFAULT_TRANSPORT
        if transport != "sdk":
            raise CodexSDKError(
                f"codex transport {transport!r} is unsupported — the official "
                "Python SDK is the only Codex provider transport"
            )
        return transport

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
        params = optional_params or {}
        clean_model = model.removeprefix("codex/").removeprefix("cdx-")
        self._resolve_transport(params)
        prompt, images = _messages_to_codex_input(messages)
        text, usage = run_sdk(
            prompt=prompt,
            images=images,
            model=clean_model,
            cwd=_resolve_codex_cwd(params),
            effort=_resolve_effort(params),
            timeout=float(timeout) if timeout else DEFAULT_TURN_TIMEOUT_S,
            call_index=_next_call_index(),
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
        params = optional_params or {}
        clean_model = model.removeprefix("codex/").removeprefix("cdx-")
        self._resolve_transport(params)
        parts: list[str] = []
        usage: dict[str, int] = {}
        prompt, images = _messages_to_codex_input(messages)
        async for chunk in astream_sdk(
            prompt=prompt,
            images=images,
            model=clean_model,
            cwd=_resolve_codex_cwd(params),
            effort=_resolve_effort(params),
            timeout=float(timeout) if timeout else DEFAULT_TURN_TIMEOUT_S,
            call_index=_next_call_index(),
        ):
            parts.append(str(chunk.get("text") or ""))
            raw_usage = chunk.get("usage")
            if isinstance(raw_usage, dict):
                usage = {
                    "input_tokens": int(raw_usage.get("prompt_tokens", 0) or 0),
                    "output_tokens": int(raw_usage.get("completion_tokens", 0) or 0),
                    "reasoning_output_tokens": int(
                        raw_usage.get("reasoning_output_tokens", 0) or 0
                    ),
                    "total_tokens": int(raw_usage.get("total_tokens", 0) or 0),
                }
        text = "".join(parts)
        return _build_model_response(text=text, model=clean_model, usage_payload=usage)

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
        # #708: clio/DSPy request streaming by default, so this MUST be a real
        # generator (NOT a coroutine). The SDK streams token deltas, but the
        # SYNC path drains them and yields one terminal chunk (DSPy drives turns
        # through astreaming; sync streaming is the compatibility fallback).
        params = optional_params or {}
        clean_model = model.removeprefix("codex/").removeprefix("cdx-")
        self._resolve_transport(params)
        prompt, images = _messages_to_codex_input(messages)
        text, usage = run_sdk(
            prompt=prompt,
            images=images,
            model=clean_model,
            cwd=_resolve_codex_cwd(params),
            effort=_resolve_effort(params),
            timeout=float(timeout) if timeout else DEFAULT_TURN_TIMEOUT_S,
            call_index=_next_call_index(),
        )
        yield GenericStreamingChunk(
            text=text,
            tool_use=None,
            is_finished=True,
            finish_reason="stop",
            usage=cast(
                ChatCompletionUsageBlock,
                usage_chunk(usage)
                or {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
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
        # #708: must be an async GENERATOR (real async iterator), not a coroutine
        # that returns one. The SDK streams real token deltas into the frozen
        # chunk pipeline.
        params = optional_params or {}
        clean_model = model.removeprefix("codex/").removeprefix("cdx-")
        self._resolve_transport(params)
        prompt, images = _messages_to_codex_input(messages)
        async for chunk in astream_sdk(
            prompt=prompt,
            images=images,
            model=clean_model,
            cwd=_resolve_codex_cwd(params),
            effort=_resolve_effort(params),
            timeout=float(timeout) if timeout else DEFAULT_TURN_TIMEOUT_S,
            call_index=_next_call_index(),
        ):
            yield chunk  # type: ignore[misc]  # dict satisfies litellm's runtime chunk contract


# The registration guard (idempotent append to `litellm.custom_provider_map`,
# once per process — without it, hot-swapping providers via PUT /v1/providers/lm
# grows the map without bound) is the shared custom-provider machinery.
ensure_registered, _reset_for_tests = register_custom_provider("codex", CodexLLM)


__all__ = [
    "CodexLLM",
    "ensure_registered",
    "astream_sdk",
    "run_sdk",
]
