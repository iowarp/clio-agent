"""What a running managed model server actually has in force, and where each value came from.

A requested server parameter is not proof of what the server runs with: an
engine may clamp it, derive it (llama.cpp splits its context across slots),
or apply its own default when nothing was requested. The deployment view
therefore shows *effective* values, each labelled with its source, preferring
the most authoritative one available:

1. ``server_report`` -- the running server's own answer (llama.cpp ``/props``,
   vLLM ``/v1/models`` and ``/metrics``, Ollama's startup ``server config``
   line and ``/api/ps``);
2. ``container_config`` -- the flags and environment the container runtime
   says the server was started with (``inspect``);
3. ``launch_request`` -- what CLIO asked for, when the runtime cannot report
   the launch (Apptainer instances);
4. ``engine_default`` -- nothing was set and the server does not report it:
   the engine's documented behaviour is shown, marked as such.

The parsers are pure functions of the payloads so they are tested against real
server output; :func:`observe_effective` only gathers the payloads.
"""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from clio_agent.gact.infrastructure.container_runtime import (
    INSPECT_SEPARATOR,
    inspect_config_command,
    logs_command,
)
from clio_agent.gact.infrastructure.models import (
    CommandResult,
    CommandSpec,
    EffectiveParameter,
    RuntimeName,
)
from clio_agent.gact.infrastructure.server_parameters import EngineId, engine_parameters
from clio_agent.gact.infrastructure.transport_text import unwrap

HttpGet = Callable[[str], Awaitable[str | None]]
RunCommand = Callable[[CommandSpec], Awaitable[CommandResult]]

#: ``(value, detail)`` pairs the server reported, keyed by parameter effective key.
ServerReport = dict[str, tuple[str, str]]


def _json(text: str | None) -> Any:
    if not text:
        return None
    try:
        return json.loads(text)
    except ValueError:
        return None


def parse_container_config(stdout: str) -> tuple[list[str] | None, dict[str, str] | None]:
    """Split ``inspect --format '{{json .Args}} |clio| {{json .Config.Env}}'`` output.

    Returns:
        The entrypoint arguments and the environment as a mapping, or
        ``(None, None)`` when the output is not the expected shape (the launch
        is then unknown, never "nothing was set").
    """

    # The output is one line, but the Desktop's terminal may hard-wrap it.
    text = unwrap(stdout)
    separator = INSPECT_SEPARATOR.strip()
    if separator not in text:
        return None, None
    raw_args, raw_env = (part.strip() for part in text.split(separator, 1))
    args, env = _json(raw_args), _json(raw_env)
    if not isinstance(args, list) or not isinstance(env, list):
        return None, None
    pairs = (str(item).split("=", 1) for item in env if "=" in str(item))
    return [str(item) for item in args], dict(pairs)


def _flag_value(args: list[str], name: str, aliases: tuple[str, ...] = ()) -> str | None:
    names = (name, *aliases)
    for index, token in enumerate(args):
        for candidate in names:
            if token == candidate and index + 1 < len(args):
                return args[index + 1]
            if token.startswith(f"{candidate}="):
                return token.split("=", 1)[1]
    return None


_FLAG_ALIASES: dict[str, tuple[str, ...]] = {
    "--parallel": ("-np",),
    "--ctx-size": ("-c",),
    "--threads": ("-t",),
    "--tensor-parallel-size": ("-tp",),
    "--pipeline-parallel-size": ("-pp",),
}


def parse_llama_props(text: str | None) -> ServerReport:
    """llama.cpp ``GET /props``: slot count and the context each request gets."""

    payload = _json(text)
    if not isinstance(payload, Mapping):
        return {}
    report: ServerReport = {}
    slots = payload.get("total_slots")
    if isinstance(slots, int) and slots > 0:
        report["parallel"] = (str(slots), "llama.cpp /props total_slots")
    settings = payload.get("default_generation_settings")
    n_ctx = settings.get("n_ctx") if isinstance(settings, Mapping) else None
    if isinstance(n_ctx, int) and n_ctx > 0:
        report["context_per_slot"] = (
            str(n_ctx),
            "llama.cpp /props default_generation_settings.n_ctx",
        )
    return report


def parse_vllm_models(text: str | None) -> ServerReport:
    """vLLM ``GET /v1/models``: the context length the server accepts."""

    payload = _json(text)
    rows = payload.get("data") if isinstance(payload, Mapping) else None
    if not isinstance(rows, list) or not rows or not isinstance(rows[0], Mapping):
        return {}
    value = rows[0].get("max_model_len")
    if isinstance(value, int) and value > 0:
        return {"max_model_len": (str(value), "vLLM /v1/models max_model_len")}
    return {}


_CACHE_INFO = re.compile(r"^vllm:cache_config_info\{(?P<labels>[^}]*)\}", re.MULTILINE)
_LABEL = re.compile(r'(\w+)="([^"]*)"')


def parse_vllm_metrics(text: str | None) -> ServerReport:
    """vLLM ``GET /metrics``: the ``cache_config_info`` labels (memory reservation)."""

    match = _CACHE_INFO.search(text or "")
    if match is None:
        return {}
    labels = dict(_LABEL.findall(match.group("labels")))
    report: ServerReport = {}
    if labels.get("gpu_memory_utilization"):
        report["gpu_memory_utilization"] = (
            labels["gpu_memory_utilization"],
            "vLLM /metrics cache_config_info",
        )
    return report


_OLLAMA_CONFIG = re.compile(r'msg="server config".*?env="map\[(?P<env>.*?)\]"')
_OLLAMA_KEYS = {"OLLAMA_NUM_PARALLEL": "num_parallel", "OLLAMA_CONTEXT_LENGTH": "context_length"}


def parse_ollama_server_config(logs: str) -> ServerReport:
    """Ollama's startup ``server config`` log line: the settings the server applied."""

    report: ServerReport = {}
    # Joined across terminal wraps: the config line is longer than the column width.
    for match in _OLLAMA_CONFIG.finditer(unwrap(logs)):
        for key, pid in _OLLAMA_KEYS.items():
            found = re.search(rf"(?:^|\s){key}:(\S*)", match.group("env"))
            if found:
                report[pid] = (found.group(1), "Ollama server config (startup log)")
    return report


def parse_ollama_ps(text: str | None) -> list[EffectiveParameter]:
    """Ollama ``GET /api/ps``: the context each loaded model actually runs with."""

    payload = _json(text)
    models = payload.get("models") if isinstance(payload, Mapping) else None
    rows: list[EffectiveParameter] = []
    for model in models if isinstance(models, list) else []:
        if not isinstance(model, Mapping):
            continue
        context = model.get("context_length")
        if isinstance(context, int) and context > 0:
            rows.append(
                EffectiveParameter(
                    id=f"loaded_context:{model.get('name', '')}",
                    label=f"Loaded context ({model.get('name', 'model')})",
                    value=str(context),
                    source="server_report",
                    detail="Ollama /api/ps context_length",
                )
            )
    return rows


def assemble(
    engine: EngineId,
    variant_id: str,
    *,
    report: ServerReport,
    container_args: list[str] | None,
    container_env: Mapping[str, str] | None,
    requested: Mapping[str, str],
) -> list[EffectiveParameter]:
    """Pick the most authoritative source for every declared parameter.

    Args:
        engine: The engine whose declaration applies.
        variant_id: The launched variant (variant-only parameters are skipped).
        report: Values the running server reported.
        container_args: The launched arguments, or ``None`` when the runtime
            cannot report them.
        container_env: The launched environment, or ``None`` likewise.
        requested: The parameter values CLIO launched with, by parameter id.

    Returns:
        One row per applicable parameter, plus server-only facts.
    """

    rows: list[EffectiveParameter] = []
    for parameter in engine_parameters(engine, variant_id):
        key = parameter.effective_key or parameter.id
        if key in report:
            value, detail = report[key]
            rows.append(
                EffectiveParameter(
                    id=parameter.id,
                    label=parameter.label,
                    value=value,
                    source="server_report",
                    detail=detail,
                )
            )
            continue
        launched: str | None = None
        if container_args is not None and parameter.delivery == "flag":
            launched = _flag_value(
                container_args, parameter.name, _FLAG_ALIASES.get(parameter.name, ())
            )
        elif container_env is not None and parameter.delivery == "env":
            launched = container_env.get(parameter.name)
        if launched is not None:
            rows.append(
                EffectiveParameter(
                    id=parameter.id,
                    label=parameter.label,
                    value=launched,
                    source="container_config",
                    detail=f"{parameter.name} in the running container",
                )
            )
        elif container_args is None and parameter.id in requested:
            rows.append(
                EffectiveParameter(
                    id=parameter.id,
                    label=parameter.label,
                    value=requested[parameter.id],
                    source="launch_request",
                    detail=f"{parameter.name} as launched by CLIO",
                )
            )
        else:
            rows.append(
                EffectiveParameter(
                    id=parameter.id,
                    label=parameter.label,
                    value=parameter.default_behavior or "Engine default",
                    source="engine_default",
                    detail=f"{parameter.name} not set",
                )
            )
    if "context_per_slot" in report:
        value, detail = report["context_per_slot"]
        rows.append(
            EffectiveParameter(
                id="context_per_slot",
                label="Context per request",
                value=value,
                source="server_report",
                detail=detail,
            )
        )
    return rows


async def observe_effective(
    engine: EngineId,
    variant_id: str,
    *,
    runtime: RuntimeName | None,
    container_name: str,
    requested: Mapping[str, str],
    http_get: HttpGet,
    run: RunCommand,
) -> list[EffectiveParameter]:
    """Gather the running server's reports and launch config, then :func:`assemble`.

    Args:
        engine: The engine.
        variant_id: The launched variant.
        runtime: The container runtime the service runs in, or ``None`` for
            a native process (nothing to inspect; launch values are as requested).
        container_name: The CLIO-owned container / instance name.
        requested: Parameter values CLIO launched with, by parameter id.
        http_get: GET a server path (``/props``) and return its body, or ``None``.
        run: Execute a command on the service's target.
    """

    report: ServerReport = {}
    extra: list[EffectiveParameter] = []
    if engine == "llama_cpp":
        report.update(parse_llama_props(await http_get("/props")))
    elif engine == "vllm":
        report.update(parse_vllm_models(await http_get("/v1/models")))
        report.update(parse_vllm_metrics(await http_get("/metrics")))
    else:
        if runtime is not None:
            logs = await run(logs_command(runtime, container_name, lines=400))
            report.update(parse_ollama_server_config(logs.stdout + logs.stderr))
        extra = parse_ollama_ps(await http_get("/api/ps"))
    args: list[str] | None = None
    env: dict[str, str] | None = None
    inspect = None if runtime is None else inspect_config_command(runtime, container_name)
    if inspect is not None:
        result = await run(inspect)
        if result.exit_code == 0:
            args, env = parse_container_config(result.stdout)
    rows = assemble(
        engine,
        variant_id,
        report=report,
        container_args=args,
        container_env=env,
        requested=requested,
    )
    return rows + extra
