"""Context sizing for CLIO-managed model servers (deployment semantics).

The engine-neutral pieces (profiles, adapters, strategies, the control's wire
shape) live in :mod:`clio_agent.context_sizing`; this module applies them to
vLLM, llama.cpp and Ollama deployments.

The value chosen here becomes the server's launch setting -- vLLM
``--max-model-len``, llama.cpp ``--ctx-size``, Ollama ``num_ctx`` (applied by
:mod:`~clio_agent.gact.infrastructure.ollama_context_apply` after the pull) --
and is recorded with its reason in the settled configuration
(``effective.context_length`` / ``effective.context_reason`` /
``effective.context_choice``).

The person's control (see :mod:`.controls`): a typed number in the engine's
context parameter, or ``context.choice`` = ``max`` / ``fit_to_gpu`` with an
optional ``context.strategy`` and ``context.gpu_share``. Nothing chosen means
Fit to GPU with the configured strategy, falling back to the model's maximum
capped at 32768 when Fit to GPU cannot be computed.

Sizing a vLLM or llama.cpp launch needs the model's layout and the GPU's free
memory, both on the execution host: the plan carries a ``before_launch`` step
that runs :mod:`.target_probe` there and rebuilds the plan with the value. The
sized parameter is a launch input, not the person's choice, so it is never
persisted as ``param.*`` (a later reinstall sizes again).
"""

from __future__ import annotations

import dataclasses
import json
import posixpath
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from clio_agent.context_sizing.adapters import gguf_profile, hf_profile
from clio_agent.context_sizing.controls import (
    CHOICE_KEY,
    SHARE_KEY,
    STRATEGY_KEY,
    ContextChoice,
    ContextControls,
    ContextSizingSpec,
    FitToGpu,
    strategy_infos,
    unavailable_fit,
)
from clio_agent.context_sizing.profile import GIB, GpuBudget, ModelMemoryProfile
from clio_agent.context_sizing.strategies import (
    configured_strategy_id,
    default_context,
    fit_unavailable_reason,
    get_strategy,
    size_with,
)
from clio_agent.gact.infrastructure.context_sizing import target_probe
from clio_agent.gact.infrastructure.models import CommandResult, CommandSpec, TargetFacts
from clio_agent.gact.infrastructure.plan import DriverPlan
from clio_agent.gact.infrastructure.server_parameters import (
    PARAMETER_PREFIX,
    EngineId,
    ServerParameter,
    engine_parameters,
)

Execute = Callable[[CommandSpec], Awaitable[CommandResult]]
Progress = Callable[[str], object]

#: The engine parameter a context length is launched with.
CONTEXT_PARAMETER: dict[EngineId, str] = {
    "vllm": "max_model_len",
    "llama_cpp": "ctx_size",
    "ollama": "context_length",
}
ENGINE_LABELS: dict[EngineId, str] = {"vllm": "vLLM", "llama_cpp": "llama.cpp", "ollama": "Ollama"}
EFFECTIVE_LENGTH = "effective.context_length"
EFFECTIVE_REASON = "effective.context_reason"
EFFECTIVE_CHOICE = "effective.context_choice"
#: Not KV cache: (weights growth factor for runtime buffers, fixed reserve per device).
RESERVE: dict[EngineId, tuple[float, int]] = {
    # Activation peak, CUDA graphs, non-torch memory (Qwen3-4B on an A40 measured
    # 2.27 GiB) plus the gap between nvidia-smi's total and torch's.
    "vllm": (1.0, int(2.75 * GIB)),
    "llama_cpp": (1.05, GIB),  # compute buffers and headroom
    "ollama": (1.25, GIB),
}
#: Variants whose KV cache lives in accelerator memory.
GPU_VARIANTS = frozenset({"cuda", "rocm", "vulkan", "native-cuda", "native-cuda-attention"})
#: vLLM's own --gpu-memory-utilization default.
VLLM_DEFAULT_UTILIZATION = 0.9
_CHOICES = ("number", "max", "fit_to_gpu")


def context_key(engine: EngineId) -> str:
    """The ``param.*`` configuration key of the engine's context parameter."""

    return PARAMETER_PREFIX + CONTEXT_PARAMETER[engine]


def _positive_int(configuration: Mapping[str, str], key: str, default: int = 1) -> int:
    raw = str(configuration.get(key, "") or "").strip()
    return int(raw) if raw.isdigit() and int(raw) > 0 else default


@dataclass(frozen=True)
class SizingRequest:
    """The person's context choice for one deployment, validated.

    Attributes:
        choice: ``number``, ``max`` or ``fit_to_gpu``.
        number: The typed context length (``number`` only).
        strategy: The Fit-to-GPU strategy id.
        share: The deployment's GPU-memory share (0-1], or None for the
            engine default (vLLM: its memory utilization; others: 1).
        sequences: Sequences the engine allocates a full context for at once.
    """

    choice: ContextChoice
    number: int | None = None
    strategy: str = ""
    share: float | None = None
    sequences: int = 1


def sizing_request(engine: EngineId, configuration: Mapping[str, str]) -> SizingRequest:
    """Read and validate the context control from a deployment configuration.

    Raises:
        ValueError: For an unknown choice or strategy, a ``number`` choice with
            no number, or a GPU share outside (0, 1].
    """

    typed = str(configuration.get(context_key(engine), "") or "").strip()
    raw_choice = str(configuration.get(CHOICE_KEY, "") or "").strip()
    if raw_choice and raw_choice not in _CHOICES:
        raise ValueError(f"Context choice must be one of {', '.join(_CHOICES)}")
    choice = cast(ContextChoice, raw_choice or ("number" if typed else "fit_to_gpu"))
    number = None
    if choice == "number":
        if not typed.isdigit():
            raise ValueError("Type a whole-number context length, or choose Max or Fit to GPU")
        number = int(typed)
    strategy = str(configuration.get(STRATEGY_KEY, "") or "").strip() or configured_strategy_id()
    get_strategy(strategy)
    share = None
    raw_share = str(configuration.get(SHARE_KEY, "") or "").strip()
    if raw_share:
        try:
            share = float(raw_share)
        except ValueError as exc:
            raise ValueError("GPU memory share must be a number between 0 and 1") from exc
        if not 0 < share <= 1:
            raise ValueError("GPU memory share must be above 0 and at most 1")
    sequences = _positive_int(configuration, PARAMETER_PREFIX + "num_parallel")
    return SizingRequest(
        choice=choice,
        number=number,
        strategy=strategy,
        share=share,
        sequences=sequences if engine == "ollama" else 1,
    )


@dataclass(frozen=True)
class ContextDecision:
    """The context a deployment launches with, and why.

    ``tokens`` is None when the engine's own default stays in force.
    """

    tokens: int | None
    reason: str
    choice: ContextChoice
    strategy: str = ""

    def entries(self) -> dict[str, str]:
        """The settled-configuration entries recording this decision."""

        entries = {
            EFFECTIVE_LENGTH: str(self.tokens) if self.tokens else "",
            EFFECTIVE_REASON: self.reason,
            EFFECTIVE_CHOICE: self.choice,
        }
        if self.strategy:
            entries["effective.context_strategy"] = self.strategy
        return entries


def decide(
    engine: EngineId,
    request: SizingRequest,
    profile: ModelMemoryProfile | None,
    budget: GpuBudget | None,
    *,
    why: str = "",
) -> ContextDecision:
    """The launch context for ``request`` given what is known about the model and GPU.

    Args:
        engine: The engine (its name appears in reasons).
        request: The person's choice.
        profile: The model's memory profile, or None when it could not be read.
        budget: The deployment's GPU budget, or None when unknown.
        why: Why the profile is missing (shown in the reason).
    """

    label = ENGINE_LABELS[engine]
    if request.choice == "number":
        return ContextDecision(request.number, f"set by you ({request.number})", "number")
    trained = profile.trained_context if profile is not None else None
    if request.choice == "max":
        if trained:
            return ContextDecision(trained, f"the model's own maximum {trained}", "max")
        return ContextDecision(
            None, f"the model's own maximum, as {label} reads it ({why or 'not read'})", "max"
        )
    missing = fit_unavailable_reason(profile, budget)
    if not missing:
        sized = size_with(request.strategy, profile, budget)
        named = get_strategy(request.strategy).label
        return ContextDecision(
            sized.tokens, f"{named}: {sized.reason}", "fit_to_gpu", request.strategy
        )
    fallback = default_context(profile, budget, request.strategy)
    if fallback is None:
        reason = why if profile is None and why else missing
        return ContextDecision(None, f"{label}'s own default: {reason}", "fit_to_gpu")
    return ContextDecision(
        fallback.tokens, f"Fit to GPU not computed; {fallback.reason}", "fit_to_gpu"
    )


def gpu_budget(
    engine: EngineId,
    variant_id: str,
    gpus: list[Mapping[str, int]] | None,
    weights_bytes: int,
    configuration: Mapping[str, str],
    request: SizingRequest,
) -> GpuBudget | None:
    """The KV budget of a vLLM or llama.cpp deployment from the host's GPU memory.

    vLLM reserves ``share`` (default: its ``--gpu-memory-utilization``) of each
    device it uses (tensor x pipeline parallel), never more than is free;
    llama.cpp splits layers over every visible GPU and gets ``share`` of their
    free memory. None for a CPU variant or when no GPU memory is reported.
    """

    if variant_id not in GPU_VARIANTS or not gpus:
        return None
    factor, reserve = RESERVE[engine]
    rows = [(int(row.get("total", 0)), int(row.get("free", 0))) for row in gpus]
    if engine == "vllm":
        devices = _positive_int(configuration, PARAMETER_PREFIX + "tensor_parallel_size")
        devices *= _positive_int(configuration, PARAMETER_PREFIX + "pipeline_parallel_size")
        used = rows[:devices]
        raw = str(configuration.get(PARAMETER_PREFIX + "gpu_memory_utilization", "") or "")
        try:
            share = request.share or float(raw or VLLM_DEFAULT_UTILIZATION)
        except ValueError:
            share = VLLM_DEFAULT_UTILIZATION
        memory = sum(min(int(total * share), free) for total, free in used)
        detail = f"{share:g} of {len(used)} GPU(s), within free memory"
    else:
        used = rows
        share = request.share or 1.0
        memory = int(sum(free for _total, free in used) * share)
        detail = f"{share:g} of the free memory on {len(used)} GPU(s)"
    if memory <= 0:
        return None
    return GpuBudget(
        memory_bytes=memory,
        reserved_bytes=int(weights_bytes * factor) + reserve * len(used),
        sequences=request.sequences,
        detail=detail,
    )


def ollama_budget(
    available_bytes: int | None, weights_bytes: int, request: SizingRequest
) -> GpuBudget | None:
    """The KV budget of a managed Ollama from the GPU memory its startup log reports."""

    if not available_bytes:
        return None
    factor, reserve = RESERVE["ollama"]
    share = request.share or 1.0
    return GpuBudget(
        memory_bytes=int(available_bytes * share),
        reserved_bytes=int(weights_bytes * factor) + reserve,
        sequences=request.sequences,
        detail=f"{share:g} of what Ollama reports available",
    )


def model_locator(engine: EngineId, configuration: Mapping[str, str]) -> tuple[str, str] | str:
    """``(kind, path)`` of the model on the execution host, or why it has none."""

    if engine == "vllm":
        model = str(configuration.get("model", "")).strip()
        if posixpath.isabs(model):
            return "hf", model
        return "the model is a Hugging Face reference that only vLLM resolves"
    if engine == "llama_cpp":
        path = str(configuration.get("model_path", "")).strip()
        if path:
            return "gguf", path
        return "the GGUF is fetched by llama.cpp itself at launch"
    return "Ollama reads the model only once it is pulled"


def probe_command(kind: str, path: str) -> CommandSpec:
    """The target-side read of the model's layout and the GPUs' memory."""

    return CommandSpec(
        program="python3",
        args=["-c", Path(target_probe.__file__).read_text(encoding="utf-8")],
        stdin=json.dumps({"kind": kind, "path": path}),
        timeout_seconds=60,
    )


def parse_probe(stdout: str) -> dict[str, Any] | None:
    """The marked JSON answer of :mod:`.target_probe`, or None."""

    for line in reversed(stdout.splitlines()):
        if line.startswith(target_probe.MARKER):
            try:
                answer = json.loads(line[len(target_probe.MARKER) :])
            except ValueError:
                return None
            return answer if isinstance(answer, dict) else None
    return None


async def probe_model(execute: Execute, kind: str, path: str) -> dict[str, Any]:
    """Run the probe on the host; a failure is an answer with an ``error``."""

    try:
        result = await execute(probe_command(kind, path))
    except (OSError, RuntimeError, ValueError) as exc:
        return {"error": f"the host could not be asked: {exc}"}
    answer = parse_probe(result.stdout) if result.exit_code == 0 else None
    if answer is None:
        detail = (result.stderr or result.stdout).strip().splitlines()
        return {"error": f"the host's answer was unreadable ({detail[-1] if detail else 'empty'})"}
    return answer


def profile_from_probe(
    engine: EngineId, answer: Mapping[str, Any]
) -> tuple[ModelMemoryProfile | None, str]:
    """The model profile from a probe answer, or why there is none."""

    weights = int(answer.get("weights_bytes") or 0)
    if engine == "vllm" and isinstance(answer.get("config"), dict):
        return hf_profile(answer["config"], weights_bytes=weights), ""
    if engine == "llama_cpp" and isinstance(answer.get("gguf"), dict):
        return gguf_profile(answer["gguf"], weights_bytes=weights), ""
    return None, str(answer.get("error") or "the model's layout could not be read")


async def gather_inputs(
    engine: EngineId,
    variant_id: str,
    configuration: Mapping[str, str],
    facts: TargetFacts,
    request: SizingRequest,
    execute: Execute,
) -> tuple[ModelMemoryProfile | None, GpuBudget | None, str]:
    """Read the profile and budget of a vLLM or llama.cpp deployment from its host."""

    located = model_locator(engine, configuration)
    if isinstance(located, str):
        return None, None, located
    if facts.os != "linux":
        return None, None, "the model and GPU are read on Linux execution hosts only"
    answer = await probe_model(execute, *located)
    profile, why = profile_from_probe(engine, answer)
    gpus = answer.get("gpus") if isinstance(answer.get("gpus"), list) else None
    weights = profile.weights_bytes if profile is not None else 0
    budget = gpu_budget(engine, variant_id, gpus, weights, configuration, request)
    return profile, budget, why


def _relaunches(action: str, variant_id: str, configuration: Mapping[str, str]) -> bool:
    """Whether ``action`` launches the server with freshly compiled arguments."""

    if action in {"install", "reinstall"}:
        return True
    native = variant_id.startswith("native")
    return action == "start" and (native or configuration.get("container_runtime") == "apptainer")


def _finish(
    build: Callable[[dict[str, str]], DriverPlan],
    engine: EngineId,
    base: dict[str, str],
    decision: ContextDecision,
) -> DriverPlan:
    """Build the plan launching ``decision`` and record it in the settled configuration."""

    key = context_key(engine)
    launch = dict(base)
    if decision.choice != "number":
        if decision.tokens:
            launch[key] = str(decision.tokens)
        elif decision.choice == "max" and engine == "llama_cpp":
            launch[key] = "0"  # llama.cpp: the model's own context
    plan = build(launch)
    settled = dict(plan.configuration or launch)
    if decision.choice != "number":
        settled.pop(key, None)  # a launch input, not the person's value
    return dataclasses.replace(plan, configuration={**settled, **decision.entries()})


def sized_model_runtime_plan(
    *,
    engine: EngineId,
    action: str,
    variant_id: str,
    configuration: dict[str, str],
    facts: TargetFacts,
    build: Callable[[dict[str, str]], DriverPlan],
) -> DriverPlan:
    """Compile a managed model server's plan with its context sized.

    Args:
        engine: The engine.
        action: The lifecycle action.
        variant_id: The variant.
        configuration: The deployment configuration.
        facts: The inspected target.
        build: Compiles the plan for a configuration (the engine driver).
    """

    if action not in {"install", "reinstall", "start"}:
        return build(configuration)
    request = sizing_request(engine, configuration)
    base = dict(configuration)
    if request.choice != "number":
        base.pop(context_key(engine), None)
    if engine == "ollama":
        # Sized after the pull (the Ollama hook); a typed number is its launch env.
        if request.choice == "number":
            return _finish(build, engine, base, decide(engine, request, None, None))
        return build(base)
    if not _relaunches(action, variant_id, configuration):
        return build(configuration)  # the container restarts as it was created
    located = model_locator(engine, base)
    if request.choice == "number" or isinstance(located, str) or facts.os != "linux":
        why = located if isinstance(located, str) else "the model is read on Linux hosts only"
        return _finish(build, engine, base, decide(engine, request, None, None, why=why))

    async def before_launch(execute: Execute, progress: Progress) -> DriverPlan:
        progress("Sizing the model's context")
        profile, budget, why = await gather_inputs(
            engine, variant_id, base, facts, request, execute
        )
        decision = decide(engine, request, profile, budget, why=why)
        if decision.tokens:
            progress(f"Serving the model with context {decision.tokens}")
        return _finish(build, engine, base, decision)

    return dataclasses.replace(build(base), before_launch=before_launch)


def deployment_controls(
    engine: EngineId,
    request: SizingRequest,
    profile: ModelMemoryProfile | None,
    budget: GpuBudget | None,
    *,
    why: str = "",
    installed: Mapping[str, str] | None = None,
) -> ContextControls:
    """The control a deployment form renders for one model on one host.

    ``current`` is what the installed deployment runs with when ``installed``
    (its settled configuration) is given, else what this configuration would
    launch with.
    """

    trained = profile.trained_context if profile is not None else None
    missing = fit_unavailable_reason(profile, budget)
    if missing:
        detail = why if profile is None and why else missing
        fit = unavailable_fit(f"Fit to GPU cannot be computed: {detail}", request.strategy)
    else:
        sized = size_with(request.strategy, profile, budget)
        fit = FitToGpu(
            available=True,
            reason=sized.reason,
            strategy=request.strategy,
            strategies=strategy_infos(),
            value=sized.tokens,
        )
    if installed and installed.get(EFFECTIVE_REASON):
        raw = str(installed.get(EFFECTIVE_LENGTH, ""))
        choice = installed.get(EFFECTIVE_CHOICE) or None
        current = int(raw) if raw.isdigit() else None
        reason = str(installed[EFFECTIVE_REASON])
    else:
        decision = decide(engine, request, profile, budget, why=why)
        current, choice, reason = decision.tokens, decision.choice, decision.reason
    return ContextControls(
        semantics="deployment",
        maximum=trained,
        maximum_reason=(
            profile.source if trained and profile else (why or "not known before launch")
        ),
        minimum=16 if engine == "vllm" else 256,
        current=current,
        current_choice=cast(ContextChoice | None, choice if choice in _CHOICES else None),
        current_reason=reason,
        fit_to_gpu=fit,
    )


def context_spec(engine: EngineId, facts: TargetFacts) -> ContextSizingSpec:
    """The context control declaration of ``engine`` on the host ``facts`` describe."""

    if engine == "ollama":
        available, reason = True, "computed from the GPU memory Ollama reports, once pulled"
    elif facts.os != "linux":
        available, reason = False, "Fit to GPU reads the model and GPU on Linux hosts only"
    elif facts.accelerator not in {"nvidia", "amd"}:
        available, reason = False, "No GPU was found on this host"
    else:
        available, reason = True, "computed from the model's layout and the free GPU memory"
    return ContextSizingSpec(
        default_strategy=configured_strategy_id(),
        strategies=strategy_infos(),
        fit_to_gpu_available=available,
        fit_to_gpu_reason=reason,
        preview_path=f"/v1/infrastructure/services/{engine}/context-sizing",
    )


def context_parameters(engine: EngineId, facts: TargetFacts) -> list[ServerParameter]:
    """The engine's declared parameters, its context parameter carrying the control."""

    spec = context_spec(engine, facts)
    return [
        row.model_copy(update={"context_sizing": spec})
        if row.id == CONTEXT_PARAMETER[engine]
        else row
        for row in engine_parameters(engine)
    ]


__all__ = [
    "CONTEXT_PARAMETER",
    "EFFECTIVE_CHOICE",
    "EFFECTIVE_LENGTH",
    "EFFECTIVE_REASON",
    "ContextDecision",
    "SizingRequest",
    "context_key",
    "context_parameters",
    "context_spec",
    "decide",
    "deployment_controls",
    "gather_inputs",
    "gpu_budget",
    "model_locator",
    "ollama_budget",
    "parse_probe",
    "probe_command",
    "probe_model",
    "profile_from_probe",
    "sized_model_runtime_plan",
    "sizing_request",
]
