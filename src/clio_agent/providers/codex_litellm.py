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

- **Two transports.** Default is ``codex exec`` subprocess (~1-2 s
  cold start; always works if the binary is on PATH). Opt-in
  ``transport="sdk"`` uses the ``openai_codex`` Python SDK in-process
  via JSON-RPC against the local app-server daemon — much faster after
  the daemon warms, but requires ``pip install 'clio-agent[codex]'``.

- **Auth lives in the CLI.** We never see the user's ChatGPT cookie
  / OpenAI key — ``codex login`` writes a token to ``~/.codex/`` and
  the CLI uses it. We just shell out.

- **No streaming.** DSPy's ``Predict`` doesn't need streaming; we
  return a non-streaming ``ModelResponse``. ``streaming()`` /
  ``astreaming()`` raise ``NotImplementedError`` for now.

- **Registration is lazy + idempotent.** ``ensure_registered()`` is
  called from ``config.create_lm()`` / ``create_router_lm()`` only when
  ``config.provider == "codex"`` — keeps Codex out of the import graph
  for tests / installs that don't use it.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

try:
    from litellm import CustomLLM
    from litellm.types.utils import Choices, Message, ModelResponse, Usage
except ImportError as e:  # pragma: no cover - litellm is a hard dep
    raise ImportError(
        "litellm must be installed to use the Codex provider"
    ) from e


CODEX_BINARY_NAME = "codex"

#: Default Codex sandbox policy when we drive it as a bare LM. Read-only
#: keeps Codex's shell/fs tools inert (they can read, not write or
#: shell out) so the agent loop has nothing to do but answer.
DEFAULT_SANDBOX = "read-only"

#: Transport mode for the CustomLLM. ``"exec"`` shells out to
#: ``codex exec`` (always works if the binary is on PATH);
#: ``"sdk"`` uses the in-process ``openai_codex`` SDK (opt-in via
#: ``pip install 'clio-agent[codex]'``).
Transport = str  # Literal["exec", "sdk"] — kept as str so callers can override freely.
DEFAULT_TRANSPORT: Transport = "exec"


class CodexCLIUnavailableError(RuntimeError):
    """Raised when the `codex` binary isn't on PATH at request time."""


class CodexExecError(RuntimeError):
    """Raised when `codex exec` returns a non-zero exit code or
    produces no last-message output."""


def _messages_to_codex_prompt(messages: list[dict[str, Any]]) -> str:
    """Flatten an OpenAI-shape message list into a single prompt.

    Codex `exec` takes a single prompt string; DSPy / LiteLLM hand us
    the canonical ``[{role, content}, ...]`` shape that openai-compat
    backends consume. We collapse it to ``ROLE: content\\n\\n``
    sections with system messages preserved at the top.
    """
    parts: list[str] = []
    for msg in messages:
        role = str(msg.get("role", "user")).upper()
        content = msg.get("content", "")
        if isinstance(content, list):
            # Vision/multimodal: flatten the text parts only.
            content = "\n".join(
                part.get("text", "")
                for part in content
                if isinstance(part, dict) and isinstance(part.get("text"), str)
            )
        elif content is None:
            content = ""
        parts.append(f"{role}: {content}")
    return "\n\n".join(parts).strip()


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
        raise CodexExecError(
            f"codex exec timed out after {timeout}s (model={model})"
        ) from e
    finally:
        # Best-effort temp cleanup; defer until after we read.
        pass

    if proc.returncode != 0:
        # Best-effort cleanup.
        last_msg_path.unlink(missing_ok=True)
        raise CodexExecError(
            f"codex exec returned {proc.returncode} for model={model}: "
            f"{(proc.stderr or proc.stdout or '').strip()[:500]}"
        )

    try:
        text = last_msg_path.read_text(encoding="utf-8").strip()
    except FileNotFoundError as e:
        raise CodexExecError(
            f"codex exec produced no output file (model={model})"
        ) from e
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
        from openai_codex import Codex  # type: ignore[import-not-found,no-redef] # noqa: PLC0415

        return Codex
    except ImportError:
        pass
    try:
        from codex_app_server import Codex  # type: ignore[import-not-found,no-redef] # noqa: PLC0415

        return Codex
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
    request_id: str | None = None,
) -> ModelResponse:
    """Wrap a Codex completion in a LiteLLM ``ModelResponse``.

    Token counts are stubbed at zero — Codex's `exec` headless mode
    doesn't surface usage in the output file. Cost-tracking callers
    fall back to the price-table heuristic in `gact/app.py:_realised_cost`.
    """
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
        usage=Usage(prompt_tokens=0, completion_tokens=0, total_tokens=0),
    )


class CodexLLM(CustomLLM):
    """LiteLLM custom handler that routes ``codex/<model>`` to ``codex exec``."""

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
        # LiteLLM passes the model with the `codex/` prefix stripped
        # already. We also strip the leading `cdx-` namespace marker
        # so the actual model id flows clean to `codex exec`.
        #
        # Why `cdx-`: LiteLLM's dispatcher short-circuits to the OpenAI
        # handler when the bare model name (after the `codex/` split)
        # matches an entry in `litellm.open_ai_chat_completion_models`
        # — and every gpt-5* / gpt-4.1* name is in that list. Wrapping
        # the model id with a `cdx-` prefix in the registry keeps the
        # bare name unrecognizable to LiteLLM's openai-detect path so
        # routing falls through to our custom handler.
        clean_model = model.removeprefix("codex/").removeprefix("cdx-")
        prompt = _messages_to_codex_prompt(messages)
        params = optional_params or {}
        sandbox = params.get("codex_sandbox", DEFAULT_SANDBOX)
        cwd = params.get("codex_cwd", os.getcwd())
        transport = (
            params.get("codex_transport")
            or os.environ.get("CLIO_CODEX_TRANSPORT")
            or DEFAULT_TRANSPORT
        )
        timeout_s = float(timeout) if timeout else 120.0
        if transport == "sdk":
            text = _run_sdk(
                prompt=prompt,
                model=clean_model,
                sandbox=sandbox,
                cwd=cwd,
                timeout=timeout_s,
            )
        elif transport == "exec":
            text = _run_exec(
                prompt=prompt,
                model=clean_model,
                sandbox=sandbox,
                cwd=cwd,
                timeout=timeout_s,
            )
        else:
            raise CodexExecError(
                f"unknown codex transport {transport!r} (expected 'exec' or 'sdk')"
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
        # The exec transport is inherently blocking. For now we just
        # call the sync path; the SDK transport (sprint #52) gets real
        # async via ``AsyncCodex``.
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


# Module-level state guards against re-appending to
# `litellm.custom_provider_map` on every `create_lm()` call. Without
# this guard, hot-swapping providers via PUT /v1/providers/lm would
# grow the map without bound.
_registered: bool = False
_handler: CodexLLM | None = None


def ensure_registered() -> None:
    """Register the Codex handler with LiteLLM exactly once per process.

    Idempotent: subsequent calls are no-ops. Callers don't need to know
    whether registration already happened.
    """
    global _registered, _handler  # noqa: PLW0603
    if _registered:
        return
    import litellm  # noqa: PLC0415 - imported lazily for fast import path

    _handler = CodexLLM()
    litellm.custom_provider_map.append(
        {"provider": "codex", "custom_handler": _handler}
    )
    _registered = True


def _reset_for_tests() -> None:
    """Drop the registration so tests can re-register with a fresh mock."""
    global _registered, _handler  # noqa: PLW0603
    if _registered:
        import litellm  # noqa: PLC0415

        litellm.custom_provider_map[:] = [
            entry
            for entry in litellm.custom_provider_map
            if entry.get("provider") != "codex"
        ]
    _registered = False
    _handler = None


__all__ = [
    "CodexCLIUnavailableError",
    "CodexExecError",
    "CodexLLM",
    "ensure_registered",
]
