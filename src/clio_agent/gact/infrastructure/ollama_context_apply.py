"""Apply a managed Ollama's context once the model is pulled.

The decision is the engine-neutral one
(:mod:`clio_agent.gact.infrastructure.context_sizing.deployment`): the person's
Max or Fit to GPU (the default), sized from the model's ``/api/show``
``model_info`` (:func:`~clio_agent.context_sizing.adapters.ollama_profile`) and
the GPU memory Ollama logged at startup. This module reads
those through target-side commands (so it works on remote targets too), then
re-creates the model under its own name with ``num_ctx`` set. Ollama keeps the
served name, its blobs and the other Modelfile parameters, and every later
``/v1`` request loads at that context without a restart or per-request options.
A typed number keeps the ``OLLAMA_CONTEXT_LENGTH`` launch setting instead.
"""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from typing import Any

from clio_agent.context_sizing.adapters import ollama_profile
from clio_agent.context_sizing.profile import GpuBudget, ModelMemoryProfile
from clio_agent.gact.infrastructure import powershell
from clio_agent.gact.infrastructure.context_sizing.deployment import (
    ContextDecision,
    SizingRequest,
    decide,
    ollama_budget,
)
from clio_agent.gact.infrastructure.models import CommandResult, CommandSpec

Execute = Callable[[CommandSpec], Awaitable[CommandResult]]
Progress = Callable[[str], object]
AfterReadyHook = Callable[[Execute, Progress], Awaitable[dict[str, str]]]

#: Ollama logs the GPU it found at startup on every backend (CUDA, ROCm, Metal):
#: ``msg="inference compute" ... total="39.4 GiB" available="38.6 GiB"``.
_GPU_AVAILABLE = re.compile(
    r'msg="(?:inference compute|gpu memory)".*?available="(?P<value>[\d.]+) ?(?P<unit>[KMGT]i?B|B)"'
)
_MODEL_INFO_KEY = re.compile(r'"model_info"\s*:\s*')
_UNITS = {"B": 1, "KiB": 1 << 10, "MiB": 1 << 20, "GiB": 1 << 30, "TiB": 1 << 40}
_UNITS.update({"KB": 10**3, "MB": 10**6, "GB": 10**9, "TB": 10**12})


def gpu_available_bytes(logs: str) -> int | None:
    """The GPU memory Ollama last reported available, or None.

    The newest line wins: an Apptainer instance log is appended across
    restarts, so earlier lines can describe an older server.
    """

    found = [
        int(float(m.group("value")) * _UNITS[m.group("unit")])
        for m in _GPU_AVAILABLE.finditer(logs)
        # The CPU's own "inference compute" line reports system RAM, not a GPU.
        if m.group("unit") in _UNITS and "library=cpu" not in m.group(0)
    ]
    return found[-1] if found else None


def model_info_from_show(text: str) -> dict[str, Any] | None:
    """``model_info`` from an ``/api/show`` reply, also when only its tail was kept.

    Command output is bounded to its last 16 000 characters; the reply leads
    with the license and Modelfile (about 29 kB for qwen3:4b), and
    ``model_info`` comes after them, so it is decoded where it starts.
    """

    whole = _json(text)
    if isinstance(whole, dict):
        info = whole.get("model_info")
        return info if isinstance(info, dict) else None
    start = _MODEL_INFO_KEY.search(text)
    if start is None:
        return None
    try:
        info, _end = json.JSONDecoder().raw_decode(text, start.end())
    except ValueError:
        return None
    return info if isinstance(info, dict) else None


def _http(url: str, body: dict[str, Any] | None, windows: bool, timeout: float) -> CommandSpec:
    """A target-side GET (or JSON POST when ``body`` is given) printing the response."""

    if windows:
        if body is None:
            script = f"(Invoke-WebRequest -UseBasicParsing {powershell.literal(url)}).Content"
        else:
            script = (
                f"(Invoke-WebRequest -UseBasicParsing -Method Post -ContentType "
                f"'application/json' -Body {powershell.literal(json.dumps(body))} "
                f"{powershell.literal(url)}).Content"
            )
        return powershell.command(script, timeout_seconds=timeout)
    args = ["-fsS", "--noproxy", "*", "-m", str(int(timeout)), url]
    if body is not None:
        # The body goes through stdin: no quoting of model names on the command line.
        args += ["-H", "Content-Type: application/json", "--data-binary", "@-"]
    return CommandSpec(
        program="curl",
        args=args,
        stdin=json.dumps(body) if body is not None else "",
        timeout_seconds=timeout + 10,
    )


def _json(text: str) -> Any:
    try:
        return json.loads(text)
    except ValueError:
        return None


def _model_size(tags: Any, model: str) -> int:
    names = {model, f"{model}:latest"}
    for row in (tags or {}).get("models", []) if isinstance(tags, dict) else []:
        if isinstance(row, dict) and (row.get("name") in names or row.get("model") in names):
            size = row.get("size")
            return size if isinstance(size, int) else 0
    return 0


async def read_ollama_inputs(
    execute: Execute,
    port: int,
    model: str,
    logs: CommandSpec,
    windows: bool,
    request: SizingRequest,
) -> tuple[ModelMemoryProfile | None, GpuBudget | None, str]:
    """The pulled model's profile and the GPU budget, read from the running Ollama.

    Returns ``(profile, budget, why)``; ``why`` says what is missing when the
    profile could not be read.
    """

    base = f"http://127.0.0.1:{port}"
    show = await execute(_http(f"{base}/api/show", {"model": model}, windows, 30))
    info = model_info_from_show(show.stdout) if show.exit_code == 0 else None
    if info is None:
        return None, None, f"/api/show gave no model_info for {model}"
    tags = await execute(_http(f"{base}/api/tags", None, windows, 30))
    weights = _model_size(_json(tags.stdout), model)
    log_text = (await execute(logs)).stdout
    profile = ollama_profile(info, weights_bytes=weights)
    budget = ollama_budget(gpu_available_bytes(log_text), weights, request)
    if not profile.trained_context:
        return profile, budget, f"{model} reports no trained context"
    return profile, budget, ""


def usable_cpus(text: str) -> int | None:
    """The CPU count ``nproc`` printed (it honours the affinity mask), or ``None``."""

    value = text.strip().splitlines()[-1].strip() if text.strip() else ""
    return int(value) if value.isdigit() and int(value) > 0 else None


def ollama_context_hook(
    port: int,
    model: str,
    logs: CommandSpec,
    windows: bool,
    request: SizingRequest,
    cpu_threads: bool = False,
) -> AfterReadyHook:
    """The after-ready step that serves ``model`` at the chosen context.

    Returns the settled-configuration entries (``effective.context_length``,
    ``effective.context_reason``, ``effective.context_choice``); the length is
    empty when the model does not report its trained context, so Ollama's own
    default stays in force.

    With ``cpu_threads`` (the CPU variant) the model also gets ``num_thread`` =
    the CPUs this process may use: Ollama otherwise sizes its pool from the
    machine's cores, which oversubscribes a Slurm/cgroup-limited allocation (F044).
    """

    async def apply(execute: Execute, progress: Progress) -> dict[str, str]:
        progress("Sizing the model's context")
        profile, budget, why = await read_ollama_inputs(
            execute, port, model, logs, windows, request
        )
        chosen: ContextDecision = decide("ollama", request, profile, budget, why=why)
        entries = chosen.entries()
        parameters: dict[str, int] = {}
        if chosen.tokens:
            parameters["num_ctx"] = chosen.tokens
        if cpu_threads and not windows:
            threads = usable_cpus((await execute(CommandSpec(program="nproc", args=[]))).stdout)
            if threads:
                parameters["num_thread"] = threads
                entries["effective.threads"] = str(threads)
                entries["effective.threads_reason"] = f"CPUs this deployment may use ({threads})"
        if not parameters:
            return entries
        progress(f"Serving {model} with " + ", ".join(f"{k} {v}" for k, v in parameters.items()))
        created = await execute(
            _http(
                f"http://127.0.0.1:{port}/api/create",
                {"model": model, "from": model, "parameters": parameters},
                windows,
                120,
            )
        )
        if created.exit_code != 0 or '"success"' not in created.stdout:
            raise RuntimeError(
                f"Could not set the context of {model} ({parameters}): "
                + (created.stderr.strip() or created.stdout.strip()[-500:] or "no answer")
            )
        return entries

    return apply
