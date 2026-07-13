"""LiteLLM ``CustomLLM`` provider for the OpenAI Codex CLI.

Routes ``dspy.LM(model="codex/<model>", ...)`` calls through the
``codex exec`` subprocess so we use the user's ChatGPT / Codex
subscription (via ``codex login``) instead of paying per-token on the
OpenAI API.

Design notes
------------

- **No agentic loop.** We invoke Codex headless (``codex exec``) with a
  read-only sandbox so its built-in shell/filesystem tools are inert.
  The agent loop terminates after one model response and writes the
  final assistant message to a file via ``-o/--output-last-message``.
  Clio's planner does the real orchestration.

- **Three transports.** Default (production) is ``transport="app_server"``:
  the native ``codex app-server`` JSON-RPC-over-stdio surface driven by
  :mod:`clio_agent.providers.codex_app_server` — a warm subprocess per
  ``(model, cwd)``, true ``item/agentMessage/delta`` token streaming, and live
  ``thread/tokenUsage/updated`` usage. ``transport="exec"`` is the legacy
  ``codex exec`` batch subprocess (the byte-identical kill-switch path, restored
  by ``CLIO_CODEX_APP_SERVER=0``). ``transport="sdk"`` uses the ``openai_codex``
  Python SDK (opt-in via ``pip install 'clio-agent[codex]'``). The bridge-level
  ``DEFAULT_TRANSPORT`` fallback (when ``optional_params`` carries none) stays
  ``exec`` so direct/unit calls keep the zero-dependency path.

- **Auth lives in the CLI.** We never see the user's ChatGPT cookie
  / OpenAI key — ``codex login`` writes a token to ``~/.codex/`` and
  the CLI uses it. We just shell out.

- **Streaming.** ``app_server`` streams real ``item/agentMessage/delta`` chunks
  into the same streamed-chunk pipeline claude_code uses (the wire/normalization
  contract is FROZEN). ``exec``/``sdk`` produce the whole answer at once, so they
  yield a single terminal ``GenericStreamingChunk`` — but ``streaming()`` /
  ``astreaming()`` MUST return a real (async) iterator either way. (Returning a
  bare coroutine is what produced the ``'coroutine' object is not an iterator``
  mid-stream fallback crash in iowarp/clio-agent#708, before any output.)

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
import subprocess
import tempfile
import time
import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

from clio_agent.providers._cli_provider import (
    messages_to_prompt,
    register_custom_provider,
)
from clio_agent.providers.codex_app_server import (
    app_server_enabled,
    transport_fallback_payload,
)
from clio_agent.providers.codex_stateful import resolve_codex_stateful_send
from clio_agent.providers.codex_stream import (
    _next_call_index,
    astream_app_server,
    run_app_server,
    usage_chunk,
)
from clio_agent.runtime.stream_audit import stream_audit, stream_audit_enabled

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

#: Transport mode for the CustomLLM. ``"exec"`` shells out to
#: ``codex exec`` (always works if the binary is on PATH);
#: ``"sdk"`` uses the in-process ``openai_codex`` SDK (opt-in via
#: ``pip install 'clio-agent[codex]'``).
Transport = str  # Literal["app_server", "exec", "sdk"] — str so callers override freely.
#: Bridge-level fallback when ``optional_params`` carries no ``codex_transport``
#: (direct/unit calls). Production rides ``config.codex_transport`` (default
#: ``app_server``); this stays ``exec`` so the zero-dependency path is the fallback.
DEFAULT_TRANSPORT: Transport = "exec"


class CodexCLIUnavailableError(RuntimeError):
    """Raised when the `codex` binary isn't on PATH at request time."""


class CodexExecError(RuntimeError):
    """Raised when `codex exec` returns a non-zero exit code or
    produces no last-message output."""


class CodexUnsupportedMultimodalError(CodexExecError):
    """Raised when Codex CLI transport receives content it would drop."""


def _messages_to_codex_prompt(messages: list[dict[str, Any]]) -> str:
    """Serialize OpenAI-shape messages into a hardened Codex prompt.

    Codex `exec` takes a single prompt string, so we cannot pass native
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


def _run_exec(
    *,
    prompt: str,
    model: str,
    sandbox: str = DEFAULT_SANDBOX,
    cwd: str | None = None,
    timeout: float | None = 120.0,
    extra_args: list[str] | None = None,
) -> str:
    """Spawn ``codex exec`` and return the final assistant message text.

    Raises:
        CodexCLIUnavailableError: ``codex`` isn't on PATH.
        CodexExecError: the subprocess failed or produced no output.
    """
    binary = _resolve_codex_binary()
    last_msg_path = Path(tempfile.gettempdir()) / f"codex-out-{uuid.uuid4().hex}.txt"
    argv = [
        binary,
        "exec",
        "--skip-git-repo-check",
        "--sandbox",
        sandbox,
        "--model",
        model,
        "--output-last-message",
        str(last_msg_path),
        # We pipe the prompt over stdin so it can be arbitrarily large
        # without hitting argv length limits, and so we don't have to
        # worry about shell quoting on Windows.
        "-",
    ]
    if extra_args:
        argv.extend(extra_args)

    # `codex exec` writes the answer to ``last_msg_path``; a single try/finally
    # guarantees that temp file is unlinked on EVERY exit — including the
    # ``TimeoutExpired`` path, which previously left it on disk (the old
    # ``finally: pass`` cleaned up nothing).
    try:
        try:
            proc = subprocess.run(
                argv,
                input=prompt,
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=cwd,
                check=False,
            )
        except subprocess.TimeoutExpired as e:
            raise CodexExecError(f"codex exec timed out after {timeout}s (model={model})") from e

        if proc.returncode != 0:
            raise CodexExecError(
                f"codex exec returned {proc.returncode} for model={model}: "
                f"{(proc.stderr or proc.stdout or '').strip()[:500]}"
            )

        try:
            text = last_msg_path.read_text(encoding="utf-8").strip()
        except FileNotFoundError as e:
            raise CodexExecError(f"codex exec produced no output file (model={model})") from e
    finally:
        last_msg_path.unlink(missing_ok=True)

    if not text:
        raise CodexExecError(f"codex exec returned empty content (model={model})")
    return text


def _import_codex_sdk() -> Any:
    """Resolve the ``Codex`` class from the optional ``[codex]`` extra.

    The package was renamed mid-2026 from ``codex_app_server`` to
    ``openai_codex``; either may exist in the wild depending on when the
    user installed. Raises a single actionable error if neither is
    importable.
    """
    try:
        from openai_codex import (
            Codex as OpenAICodex,  # type: ignore[import-not-found] # noqa: PLC0415
        )

        return OpenAICodex
    except ImportError:
        pass
    try:
        from codex_app_server import (
            Codex as CodexAppServerCodex,  # type: ignore[import-not-found] # noqa: PLC0415
        )

        return CodexAppServerCodex
    except ImportError as e:
        raise CodexCLIUnavailableError(
            "openai_codex SDK is not installed. Install the optional "
            "extra with: pip install 'clio-agent[codex]'"
        ) from e


def _run_sdk(
    *,
    prompt: str,
    model: str,
    sandbox: str = DEFAULT_SANDBOX,
    cwd: str | None = None,
    timeout: float | None = 120.0,
) -> str:
    """In-process Codex call via the openai_codex Python SDK.

    Requires the optional ``[codex]`` extra
    (``pip install 'clio-agent[codex]'``). The SDK talks to the local
    Codex app-server daemon over JSON-RPC, so per-call latency is
    much lower than ``codex exec`` once the daemon warms.

    Raises:
        CodexCLIUnavailableError: ``openai_codex`` isn't importable
            (with an actionable install hint).
        CodexExecError: the thread returned no final response.
    """
    # The package ships as ``openai_codex`` after a 2026 rename. Earlier
    # docs called it ``codex_app_server``; either name may show up in
    # the wild, so try both before raising the actionable error.
    Codex = _import_codex_sdk()

    sandbox_kwargs: dict[str, Any] = {"sandbox": sandbox} if sandbox else {}
    with Codex() as codex:
        thread = codex.thread_start(
            model=model,
            cwd=cwd,
            ephemeral=True,
            **sandbox_kwargs,
        )
        # The SDK's `run` blocks until the agent settles. Read-only
        # sandbox + no instructions = a single model turn that produces
        # the final answer. The optional `effort` knob keeps reasoning
        # budgets tight.
        result = thread.run(prompt)

    text = (getattr(result, "final_response", "") or "").strip()
    if not text:
        raise CodexExecError(f"codex SDK returned empty content (model={model})")
    return text


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
    ``app_server`` transport; ``None`` (the ``exec``/``sdk`` batch paths) stubs
    zeros — those transports surface no usage on the output file, so cost-tracking
    callers fall back to the price-table heuristic in ``gact/app.py``. Codex's
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
    """LiteLLM custom handler that routes ``codex/<model>`` to ``codex exec``."""

    @staticmethod
    def _resolve_transport(params: dict) -> str:
        """Resolve the effective transport, applying the app-server kill-switch.

        Transport travels per-LM in ``optional_params`` (carried on the resolved
        ``LMProviderConfig``, #818); no process-global env fallback so concurrent
        experts each get their own transport. When the resolved transport is
        ``app_server`` but the kill-switch (``CLIO_CODEX_APP_SERVER=0``) is off, it
        degrades to ``exec`` — restoring the legacy batch path byte-for-byte, and
        emitting the typed ``app_server_kill_switch`` downgrade reason (audit row
        + log) so the re-route is queryable, never silent (#775).
        """
        transport = params.get("codex_transport") or DEFAULT_TRANSPORT
        if transport == "app_server" and not app_server_enabled():
            payload = transport_fallback_payload("app_server_kill_switch")
            logger.warning(
                "codex transport downgraded app_server->exec reason=%s", payload["reason"]
            )
            if stream_audit_enabled():
                stream_audit(
                    "provider.transport_fallback",
                    provider="codex_app_server",
                    transport="exec",
                    **payload,
                )
            return "exec"
        return transport

    def _complete_text(
        self,
        *,
        model: str,
        messages: list,
        optional_params: dict | None,
        timeout: Any = None,
        transport: str | None = None,
    ) -> str:
        """Run one blocking Codex turn (exec/sdk) and return the assistant text.

        The ``app_server`` transport is handled by ``completion`` / ``astreaming``
        directly (it also carries usage); this covers the batch exec/sdk paths.
        """
        # LiteLLM passes the model with the `codex/` prefix stripped already; we
        # also strip the leading `cdx-` namespace marker (see config._resolve_model_name)
        # so the actual model id flows clean to the transport.
        clean_model = model.removeprefix("codex/").removeprefix("cdx-")
        prompt = _messages_to_codex_prompt(messages)
        params = optional_params or {}
        sandbox = params.get("codex_sandbox", DEFAULT_SANDBOX)
        cwd = params.get("codex_cwd", os.getcwd())
        transport = transport or self._resolve_transport(params)
        if params.get("codex_reasoning_effort"):
            # Only app_server pins effort on turn/start. On the batch paths
            # (explicit exec/sdk config or the kill-switch downgrade) the #895
            # knob is INACTIVE — say so typed, never drop it silently.
            logger.warning(
                "codex_reasoning_effort=%s is inactive on the %r transport "
                "reason=effort_knob_inactive_on_batch_path (only app_server pins "
                "reasoning effort on turn/start)",
                params["codex_reasoning_effort"],
                transport,
            )
        timeout_s = float(timeout) if timeout else 120.0
        if transport == "sdk":
            return _run_sdk(
                prompt=prompt, model=clean_model, sandbox=sandbox, cwd=cwd, timeout=timeout_s
            )
        if transport == "exec":
            return _run_exec(
                prompt=prompt, model=clean_model, sandbox=sandbox, cwd=cwd, timeout=timeout_s
            )
        raise CodexExecError(
            f"unknown codex transport {transport!r} (expected 'app_server', 'exec' or 'sdk')"
        )

    @staticmethod
    def _final_stream_chunk(text: str) -> GenericStreamingChunk:
        """The whole Codex answer as a single terminal streaming chunk.

        Codex ``exec`` has no incremental output, so streaming is one chunk
        with ``is_finished=True``. Returning a proper ``GenericStreamingChunk``
        from an iterator (not a coroutine) is what avoids #708.
        """
        return GenericStreamingChunk(
            text=text,
            tool_use=None,
            is_finished=True,
            finish_reason="stop",
            usage={"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            index=0,
        )

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
        transport = self._resolve_transport(params)
        if transport == "app_server":
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
        text = self._complete_text(
            model=model,
            messages=messages,
            optional_params=params,
            timeout=timeout,
            transport=transport,
        )
        return _build_model_response(text=text, model=clean_model)

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
        transport = self._resolve_transport(params)
        if transport == "app_server":
            # Resolve off the loop too: the engaged path does a blocking thread/start.
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
        # The exec/SDK transports are blocking; run off the event loop so we
        # don't stall the server while Codex thinks.
        text = await asyncio.to_thread(
            self._complete_text,
            model=model,
            messages=messages,
            optional_params=params,
            timeout=timeout,
            transport=transport,
        )
        return _build_model_response(text=text, model=clean_model)

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
        # generator (NOT a coroutine). The exec/sdk transports have no incremental
        # output → one terminal chunk. app_server streams token deltas, but the
        # SYNC path drains them and yields one terminal chunk (dspy drives turns
        # through astreaming; sync streaming is the compatibility fallback).
        params = optional_params or {}
        clean_model = model.removeprefix("codex/").removeprefix("cdx-")
        transport = self._resolve_transport(params)
        if transport == "app_server":
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
            return
        text = self._complete_text(
            model=model,
            messages=messages,
            optional_params=params,
            timeout=timeout,
            transport=transport,
        )
        yield self._final_stream_chunk(text)

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
        # chunk pipeline; exec/sdk run the blocking transport off the event loop
        # and yield one final chunk.
        params = optional_params or {}
        clean_model = model.removeprefix("codex/").removeprefix("cdx-")
        transport = self._resolve_transport(params)
        if transport == "app_server":
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
            return
        text = await asyncio.to_thread(
            self._complete_text,
            model=model,
            messages=messages,
            optional_params=params,
            timeout=timeout,
            transport=transport,
        )
        yield self._final_stream_chunk(text)


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
