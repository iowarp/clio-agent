"""Apply the managed Ollama context default once the model is pulled.

The default itself is computed by :mod:`ollama_context`; this module reads
what it needs from the running server through target-side commands (so it
works on remote targets too), then re-creates the model under its own name
with ``num_ctx`` set. Ollama keeps the served name, its blobs and the other
Modelfile parameters, and every later ``/v1`` request loads at that context
without a restart or per-request options.
"""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from typing import Any

from clio_agent.gact.infrastructure import powershell
from clio_agent.gact.infrastructure.models import CommandResult, CommandSpec
from clio_agent.gact.infrastructure.ollama_context import ContextDefault, ollama_context_default

Execute = Callable[[CommandSpec], Awaitable[CommandResult]]
Progress = Callable[[str], None]
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
        if m.group("unit") in _UNITS
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


def ollama_context_hook(port: int, model: str, logs: CommandSpec, windows: bool) -> AfterReadyHook:
    """The after-ready step that serves ``model`` at its computed context.

    Returns the settled-configuration entries ``effective.context_length`` and
    ``effective.context_reason``; only the reason when Ollama does not report
    the model's trained context, so Ollama's own default stays in force.
    """

    base = f"http://127.0.0.1:{port}"

    async def apply(execute: Execute, progress: Progress) -> dict[str, str]:
        progress("Sizing the model's context")
        show = await execute(_http(f"{base}/api/show", {"model": model}, windows, 30))
        info = model_info_from_show(show.stdout) if show.exit_code == 0 else None
        if info is None:
            return {
                "effective.context_reason": f"Ollama's default: /api/show gave no model_info for {model}"
            }
        tags = await execute(_http(f"{base}/api/tags", None, windows, 30))
        log_text = (await execute(logs)).stdout
        chosen: ContextDefault | None = ollama_context_default(
            info, gpu_available_bytes(log_text), _model_size(_json(tags.stdout), model)
        )
        if chosen is None:
            return {
                "effective.context_reason": f"Ollama's default: {model} reports no trained context"
            }
        progress(f"Serving {model} with context {chosen.tokens}")
        created = await execute(
            _http(
                f"{base}/api/create",
                {"model": model, "from": model, "parameters": {"num_ctx": chosen.tokens}},
                windows,
                120,
            )
        )
        if created.exit_code != 0 or '"success"' not in created.stdout:
            raise RuntimeError(
                f"Could not set the context of {model} to {chosen.tokens}: "
                + (created.stderr.strip() or created.stdout.strip()[-500:] or "no answer")
            )
        return {
            "effective.context_length": str(chosen.tokens),
            "effective.context_reason": chosen.reason,
        }

    return apply
