"""LiteLLM ``CustomLLM`` provider for the OpenAI Codex CLI.

Routes ``dspy.LM(model="codex/<model>", ...)`` calls through the native
``codex app-server`` so we use the user's ChatGPT / Codex subscription
(via ``codex login``) instead of paying per-token on the OpenAI API.

Design notes
------------

- **No agentic loop.** We drive Codex with a read-only sandbox so its
  built-in shell/filesystem tools are inert; each turn produces only an
  answer. Clio's planner does the real orchestration.

- **One transport (v0.8.0).** ``transport="app_server"``: the native
  ``codex app-server`` JSON-RPC-over-stdio surface driven by
  :mod:`clio_agent.providers.codex_app_server` — a warm subprocess per
  ``(model, cwd)``, true ``item/agentMessage/delta`` token streaming, and live
  ``thread/tokenUsage/updated`` usage. The legacy ``exec`` batch subprocess and
  the ``sdk`` transport were DELETED (batch has no streaming and no TTFT; the
  stale preset marker for it silently steered provider swaps onto it).

- **Auth lives in the CLI.** We never see the user's ChatGPT cookie
  / OpenAI key — ``codex login`` writes a token to ``~/.codex/`` and
  the CLI uses it. We just shell out.

- **Streaming.** ``app_server`` streams real ``item/agentMessage/delta`` chunks
  into the same streamed-chunk pipeline claude_code uses (the wire/normalization
  contract is FROZEN). ``streaming()`` / ``astreaming()`` MUST return a real
  (async) iterator (a bare coroutine produced the #708 mid-stream crash).

- **Registration is lazy + idempotent.** ``ensure_registered()`` is
  called from ``config.create_lm()`` / ``create_planner_lm()`` only when
  ``config.provider == "codex"`` — keeps Codex out of the import graph
  for tests / installs that don't use it.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import time
import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any

from clio_agent.providers._cli_provider import (
    messages_to_prompt,
    register_custom_provider,
)
from clio_agent.providers.codex_stateful import resolve_codex_stateful_send
from clio_agent.providers.codex_stream import (
    _next_call_index,
    astream_app_server,
    run_app_server,
    usage_chunk,
)

logger = logging.getLogger(__name__)

try:
    from litellm import CustomLLM
    from litellm.types.utils import (
        Choices,
        GenericStreamingChunk,
        Message,
        ModelResponse,
        Usage,
    )
except ImportError as e:  # pragma: no cover - litellm is a hard dep
    raise ImportError("litellm must be installed to use the Codex provider") from e


CODEX_BINARY_NAME = "codex"

#: Default Codex sandbox policy when we drive it as a bare LM. Read-only
#: keeps Codex's shell/fs tools inert (they can read, not write or
#: shell out) so the agent loop has nothing to do but answer.
DEFAULT_SANDBOX = "read-only"

#: The ONE transport (v0.8.0): the persistent streaming app-server.
Transport = str
DEFAULT_TRANSPORT: Transport = "app_server"


class CodexCLIUnavailableError(RuntimeError):
    """Raised when the `codex` binary isn't on PATH at request time."""


class CodexExecError(RuntimeError):
    """Raised when `codex exec` returns a non-zero exit code or
    produces no last-message output."""


class CodexUnsupportedMultimodalError(CodexExecError):
    """Raised when Codex CLI transport receives content it would drop."""


def _messages_to_codex_prompt(messages: list[dict[str, Any]]) -> str:
    """Serialize OpenAI-shape messages into a hardened Codex prompt.

    Codex takes a single prompt string per turn, so we cannot pass native
    chat messages through. Thin wrapper over the shared CLI-provider
    serializer (:func:`clio_agent.providers._cli_provider.messages_to_prompt`)
    with Codex's own unsupported-multimodal exception + transport label.
    """
    return messages_to_prompt(
        messages,
        unsupported_multimodal_exc=CodexUnsupportedMultimodalError,
        transport_label="Codex",
    )


def _resolve_codex_binary() -> str:
    """Return an absolute path to the ``codex`` binary or raise.

    On Windows the npm shim ships as ``codex`` (a bash wrapper) +
    ``codex.cmd`` (the launchable Win32 shim). ``shutil.which("codex")``
    returns the bare wrapper, which ``subprocess.run`` can't exec without
    ``shell=True`` (WinError 193). Prefer the ``.cmd`` variant when it
    exists so the subprocess invocation stays shell-free.
    """
    if os.name == "nt":
        cmd_path = shutil.which(f"{CODEX_BINARY_NAME}.cmd")
        if cmd_path:
            return cmd_path
    path = shutil.which(CODEX_BINARY_NAME)
    if not path:
        raise CodexCLIUnavailableError(
            f"`{CODEX_BINARY_NAME}` not found on PATH. Install the Codex CLI "
            f"(`npm install -g @openai/codex` or `brew install --cask codex`) "
            f"and run `codex login` once per machine."
        )
    return path


def _build_model_response(
    *,
    text: str,
    model: str,
    usage_payload: dict[str, Any] | None = None,
    request_id: str | None = None,
) -> ModelResponse:
    """Wrap a Codex completion in a LiteLLM ``ModelResponse``.

    ``usage_payload`` is the normalized codex breakdown
    (:func:`clio_agent.providers.codex_app_server.normalize_usage`) from the
    ``app_server`` transport; ``None`` stubs zeros (cost-tracking callers fall
    back to the price-table heuristic in ``gact/app.py``). Codex's
    ``input_tokens`` already includes the cached subset and ``output_tokens``
    already includes reasoning, so we do NOT re-sum them (that would double-count).
    """
    usage_payload = usage_payload or {}
    prompt_tokens = int(usage_payload.get("input_tokens", 0) or 0)
    completion_tokens = int(usage_payload.get("output_tokens", 0) or 0)
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
        ),
    )


def _resolve_effort(params: dict[str, Any]) -> str | None:
    """Resolve the codex reasoning effort from the #895 thinking plan.

    ``codex_reasoning_effort`` is set by ``providers.thinking.resolve_thinking``
    (off→``none``, low/medium/high pass through). ``None`` means the knob was
    unset — no effort is pinned and codex uses its own default. This is the fix
    for the silent no-op: a requested level now reaches ``turn/start``.
    """
    effort = params.get("codex_reasoning_effort")
    return str(effort) if effort else None


def _app_server_send(
    messages: list,
    clean_model: str,
    params: dict[str, Any],
    timeout: Any,
) -> tuple[Any, str, int]:
    """Resolve the stateful-delta send plan + full prompt + call index for a call (#891).

    Returns ``(send, full_prompt, call_index)``. ``send`` is a ``CodexStatefulSend``
    (inert unless the ``stateful_delta`` flag is ON + a ReActV2 scope is active — then
    it opens/reuses a persistent thread and carries the delta bytes); ``full_prompt``
    is the serialized full prompt the transport fingerprints for cache-prefix stability;
    ``call_index`` is minted ONCE here and passed to BOTH the stateful audit row (inside
    resolve) and the transport's ``emit_call_started`` so they share one index. Blocking
    (the engaged path does a ``thread/start`` I/O on a full send), so async callers wrap
    this in ``asyncio.to_thread``.
    """
    full_prompt = _messages_to_codex_prompt(messages)
    call_index = _next_call_index()
    send = resolve_codex_stateful_send(
        messages=list(messages or []),
        full_prompt=full_prompt,
        model=clean_model,
        cwd=params.get("codex_cwd", os.getcwd()),
        effort=_resolve_effort(params),
        serialize=_messages_to_codex_prompt,
        start_timeout=float(timeout) if timeout else 180.0,
        call_index=call_index,
    )
    return send, full_prompt, call_index


class CodexLLM(CustomLLM):
    """LiteLLM custom handler that routes ``codex/<model>`` to the codex app-server."""

    @staticmethod
    def _resolve_transport(params: dict) -> str:
        """Resolve the effective transport (app_server, or a typed hard error).

        Transport travels per-LM in ``optional_params`` (carried on the resolved
        ``LMProviderConfig``, #818); no process-global env fallback so concurrent
        experts each get their own transport. The ``exec``/``sdk`` batch
        transports and the ``CLIO_CODEX_APP_SERVER`` kill-switch were deleted in
        the v0.8.0 cleanup — anything but ``app_server`` raises.
        """
        transport = params.get("codex_transport") or DEFAULT_TRANSPORT
        if transport != "app_server":
            raise CodexExecError(
                f"codex transport {transport!r} was removed in the v0.8.0 cleanup — "
                "app_server (persistent streaming) is the only transport; unset "
                "CLIO_CODEX_TRANSPORT / lm.codex_transport"
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
        self._resolve_transport(params)  # raises on anything but app_server
        send, full_prompt, call_index = _app_server_send(
            messages, clean_model, params, timeout
        )
        text, usage = run_app_server(
            prompt=full_prompt,
            model=clean_model,
            cwd=params.get("codex_cwd", os.getcwd()),
            effort=_resolve_effort(params),
            timeout=float(timeout) if timeout else 180.0,
            call_index=call_index,
            send=send,
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
        self._resolve_transport(params)  # raises on anything but app_server
        # Resolve off the loop: the engaged path does a blocking thread/start.
        send, full_prompt, call_index = await asyncio.to_thread(
            _app_server_send, messages, clean_model, params, timeout
        )
        text, usage = await asyncio.to_thread(
            run_app_server,
            prompt=full_prompt,
            model=clean_model,
            cwd=params.get("codex_cwd", os.getcwd()),
            effort=_resolve_effort(params),
            timeout=float(timeout) if timeout else 180.0,
            call_index=call_index,
            send=send,
        )
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
        # generator (NOT a coroutine). app_server streams token deltas, but the
        # SYNC path drains them and yields one terminal chunk (dspy drives turns
        # through astreaming; sync streaming is the compatibility fallback).
        params = optional_params or {}
        clean_model = model.removeprefix("codex/").removeprefix("cdx-")
        self._resolve_transport(params)  # raises on anything but app_server
        send, full_prompt, call_index = _app_server_send(
            messages, clean_model, params, timeout
        )
        text, usage = run_app_server(
            prompt=full_prompt,
            model=clean_model,
            cwd=params.get("codex_cwd", os.getcwd()),
            effort=_resolve_effort(params),
            timeout=float(timeout) if timeout else 180.0,
            call_index=call_index,
            send=send,
        )
        yield GenericStreamingChunk(
            text=text,
            tool_use=None,
            is_finished=True,
            finish_reason="stop",
            usage=usage_chunk(usage)
            or {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
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
        # that returns one. app_server streams real token deltas into the frozen
        # chunk pipeline.
        params = optional_params or {}
        clean_model = model.removeprefix("codex/").removeprefix("cdx-")
        self._resolve_transport(params)  # raises on anything but app_server
        # Resolve off the loop: the engaged path does a blocking thread/start.
        send, full_prompt, call_index = await asyncio.to_thread(
            _app_server_send, messages, clean_model, params, timeout
        )
        async for chunk in astream_app_server(
            prompt=full_prompt,
            model=clean_model,
            cwd=params.get("codex_cwd", os.getcwd()),
            effort=_resolve_effort(params),
            timeout=float(timeout) if timeout else 180.0,
            call_index=call_index,
            send=send,
        ):
            yield chunk  # type: ignore[misc]  # dict satisfies litellm's runtime chunk contract


# The registration guard (idempotent append to `litellm.custom_provider_map`,
# once per process — without it, hot-swapping providers via PUT /v1/providers/lm
# grows the map without bound) is the shared CLI-provider machinery.
ensure_registered, _reset_for_tests = register_custom_provider("codex", CodexLLM)


__all__ = [
    "CodexCLIUnavailableError",
    "CodexExecError",
    "CodexLLM",
    "ensure_registered",
    "astream_app_server",
    "run_app_server",
]
