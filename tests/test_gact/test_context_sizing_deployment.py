"""Deployment semantics: the sized context becomes the managed server's launch setting."""

from __future__ import annotations

import asyncio
import json

import pytest

from clio_agent.context_sizing.profile import GIB, GpuBudget, ModelMemoryProfile
from clio_agent.gact.infrastructure.context_sizing import target_probe
from clio_agent.gact.infrastructure.context_sizing.deployment import (
    EFFECTIVE_CHOICE,
    EFFECTIVE_LENGTH,
    EFFECTIVE_REASON,
    SizingRequest,
    decide,
    deployment_controls,
    gpu_budget,
    sizing_request,
)
from clio_agent.gact.infrastructure.drivers import build_driver_plan, service_definitions
from clio_agent.gact.infrastructure.models import (
    CommandResult,
    CommandSpec,
    ContainerRuntimeFact,
    TargetFacts,
    TargetIdentity,
)
from clio_agent.gact.infrastructure.plan import DriverPlan
from tests.test_gact.test_context_sizing_adapters import QWEN3_4B_CONFIG

QWEN3_GGUF_METADATA = {
    "general.architecture": "qwen3",
    "qwen3.context_length": 40960,
    "qwen3.block_count": 36,
    "qwen3.attention.head_count": 32,
    "qwen3.attention.head_count_kv": 8,
    "qwen3.attention.key_length": 128,
    "qwen3.attention.value_length": 128,
}
WEIGHTS = 8_044_936_192
A100_40GB = [{"total": 40 * GIB, "free": 39 * GIB}]


def _facts(accelerator: str = "nvidia", os: str = "linux") -> TargetFacts:
    return TargetFacts(
        target_id="ares",
        label="ares",
        os=os,
        arch="x86_64",
        accelerator=accelerator,
        docker_installed=True,
        docker_available=True,
        transport_state="connected",
        container_runtimes=[ContainerRuntimeFact(name="docker", installed=True, usable=True)],
        identity=TargetIdentity(uid=1008, gid=65534),
        home="/home/alice",
    )


def _plan(service: str, variant: str, configuration: dict[str, str], **kw) -> DriverPlan:
    return build_driver_plan(
        service_id=service,
        action=kw.pop("action", "install"),
        variant_id=variant,
        configuration=configuration,
        facts=kw.pop("facts", _facts()),
    )


def _launch_args(plan: DriverPlan) -> list[str]:
    return next(
        spec.args for spec in plan.commands if spec.program == "docker" and spec.args[:1] == ["run"]
    )


class FakeHost:
    """Answers the sizing probe like a GPU host would, recording what it was asked."""

    def __init__(self, answer: dict | None, exit_code: int = 0) -> None:
        self.answer = answer
        self.exit_code = exit_code
        self.requests: list[dict] = []

    async def __call__(self, spec: CommandSpec) -> CommandResult:
        assert spec.program == "python3" and target_probe.MARKER.strip() in spec.args[-1]
        self.requests.append(json.loads(spec.stdin))
        if self.answer is None:
            return CommandResult(exit_code=self.exit_code, stdout="", stderr="python3: not found")
        line = target_probe.MARKER + json.dumps(self.answer)
        return CommandResult(exit_code=self.exit_code, stdout=f"noise\n{line}\n", stderr="")


def _sized(plan: DriverPlan, host: FakeHost) -> tuple[DriverPlan, list[str]]:
    assert plan.before_launch is not None
    progress: list[str] = []
    sized = asyncio.run(plan.before_launch(host, progress.append))
    return sized, progress


def test_vllm_default_fits_the_context_to_the_gpu_and_launches_it() -> None:
    plan = _plan("vllm", "cuda", {"model": "/data/models/qwen3-4b"})
    assert "--max-model-len" not in _launch_args(plan)  # sized only once the host answers
    host = FakeHost({"gpus": A100_40GB, "weights_bytes": WEIGHTS, "config": QWEN3_4B_CONFIG})
    sized, progress = _sized(plan, host)

    assert host.requests == [{"kind": "hf", "path": "/data/models/qwen3-4b"}]
    # vLLM reserves 0.9 x 40 GiB; weights and 2 GiB of runtime memory are not KV cache.
    kv_budget = int(40 * GIB * 0.9) - WEIGHTS - 2 * GIB
    tokens = (kv_budget // 147_456) // 4096 * 4096
    args = _launch_args(sized)
    assert args[args.index("--max-model-len") + 1] == str(tokens) == "192512"
    assert sized.configuration is not None
    assert "param.max_model_len" not in sized.configuration  # a launch input, not a choice
    assert sized.configuration[EFFECTIVE_LENGTH] == "192512"
    assert sized.configuration[EFFECTIVE_CHOICE] == "fit_to_gpu"
    assert sized.configuration[EFFECTIVE_REASON].startswith(
        "Fit to GPU: trained context 262144 capped to 192512"
    )
    assert progress[-1] == "Serving the model with context 192512"
    assert sized.before_launch is None


def test_llama_cpp_keeps_the_trained_context_when_it_fits() -> None:
    plan = _plan("llama_cpp", "cuda", {"model_path": "/models/qwen3-4b.gguf"})
    host = FakeHost(
        {
            "gpus": [{"total": 24 * GIB, "free": 20 * GIB}],
            "weights_bytes": 2_500_000_000,
            "gguf": QWEN3_GGUF_METADATA,
        }
    )
    sized, _ = _sized(plan, host)
    assert host.requests == [{"kind": "gguf", "path": "/models/qwen3-4b.gguf"}]
    args = _launch_args(sized)
    assert args[args.index("--ctx-size") + 1] == "40960"
    assert sized.configuration[EFFECTIVE_REASON] == "Fit to GPU: model's trained context 40960"


def test_a_cpu_variant_has_no_gpu_budget_and_caps_the_context() -> None:
    plan = _plan("vllm", "cpu", {"model": "/data/models/qwen3-4b"})
    host = FakeHost({"gpus": A100_40GB, "weights_bytes": WEIGHTS, "config": QWEN3_4B_CONFIG})
    sized, _ = _sized(plan, host)
    args = _launch_args(sized)
    assert args[args.index("--max-model-len") + 1] == "32768"
    assert "no GPU memory budget" in sized.configuration[EFFECTIVE_REASON]


def test_an_unreadable_host_keeps_the_engines_default_and_says_why() -> None:
    plan = _plan("vllm", "cuda", {"model": "/data/models/qwen3-4b"})
    sized, _ = _sized(plan, FakeHost(None, exit_code=127))
    assert "--max-model-len" not in _launch_args(sized)
    assert sized.configuration[EFFECTIVE_LENGTH] == ""
    assert sized.configuration[EFFECTIVE_REASON] == (
        "vLLM's own default: the host's answer was unreadable (python3: not found)"
    )


def test_a_typed_number_is_launched_and_kept_as_the_persons_choice() -> None:
    plan = _plan("vllm", "cuda", {"model": "/data/models/q", "param.max_model_len": "4096"})
    assert plan.before_launch is None
    args = _launch_args(plan)
    assert args[args.index("--max-model-len") + 1] == "4096"
    assert plan.configuration["param.max_model_len"] == "4096"
    assert plan.configuration[EFFECTIVE_REASON] == "set by you (4096)"
    assert plan.configuration[EFFECTIVE_CHOICE] == "number"


def test_max_needs_no_gpu_knowledge() -> None:
    no_gpu = _facts("none")
    vllm = _plan("vllm", "cpu", {"model": "Qwen/Qwen3-4B", "context.choice": "max"}, facts=no_gpu)
    assert vllm.before_launch is None and "--max-model-len" not in _launch_args(vllm)
    assert vllm.configuration[EFFECTIVE_CHOICE] == "max"
    assert vllm.configuration[EFFECTIVE_REASON].startswith(
        "the model's own maximum, as vLLM reads it"
    )
    llama = _plan(
        "llama_cpp",
        "cpu",
        {"hf_model": "Qwen/Qwen3-4B-GGUF:Q4_K_M", "context.choice": "max"},
        facts=no_gpu,
    )
    args = _launch_args(llama)
    assert args[args.index("--ctx-size") + 1] == "0"  # llama.cpp: the model's own context
    assert "param.ctx_size" not in llama.configuration


def test_choosing_max_or_fit_drops_an_installed_number() -> None:
    plan = _plan(
        "vllm",
        "cuda",
        {"model": "/data/models/q", "param.max_model_len": "4096", "context.choice": "fit_to_gpu"},
    )
    assert plan.before_launch is not None
    assert "--max-model-len" not in _launch_args(plan)


def test_a_docker_start_reuses_the_container_as_created() -> None:
    installed = _plan("vllm", "cuda", {"model": "Qwen/Qwen3-4B"}).configuration
    assert installed is not None
    started = _plan("vllm", "cuda", dict(installed), action="start")
    assert started.before_launch is None
    assert started.configuration[EFFECTIVE_REASON] == installed[EFFECTIVE_REASON]


def test_invalid_choices_are_refused_by_name() -> None:
    with pytest.raises(ValueError, match="Unknown context sizing strategy 'nope'"):
        _plan("vllm", "cuda", {"model": "/m", "context.strategy": "nope"})
    with pytest.raises(ValueError, match="Context choice must be one of"):
        sizing_request("vllm", {"context.choice": "huge"})
    with pytest.raises(ValueError, match="whole-number context length"):
        sizing_request("vllm", {"context.choice": "number"})
    with pytest.raises(ValueError, match="at most 1"):
        sizing_request("llama_cpp", {"context.gpu_share": "1.5"})


def test_gpu_budget_follows_each_engines_memory_model() -> None:
    request = SizingRequest(choice="fit_to_gpu", strategy="fit_to_gpu")
    gpus = [{"total": 80 * GIB, "free": 70 * GIB}, {"total": 80 * GIB, "free": 80 * GIB}]
    vllm = gpu_budget("vllm", "cuda", gpus, WEIGHTS, {"param.tensor_parallel_size": "2"}, request)
    assert vllm is not None
    assert vllm.memory_bytes == 70 * GIB + int(80 * GIB * 0.9)  # never more than is free
    assert vllm.reserved_bytes == WEIGHTS + 2 * 2 * GIB
    shared = SizingRequest(choice="fit_to_gpu", strategy="fit_to_gpu", share=0.5)
    llama = gpu_budget("llama_cpp", "cuda", gpus, 1000, {}, shared)
    assert llama is not None and llama.memory_bytes == 75 * GIB
    assert gpu_budget("llama_cpp", "cpu", gpus, 1000, {}, request) is None
    assert gpu_budget("vllm", "cuda", [], WEIGHTS, {}, request) is None


def test_decisions_name_their_reason() -> None:
    request = SizingRequest(choice="fit_to_gpu", strategy="fit_to_gpu")
    profile = ModelMemoryProfile(262144, 147_456)
    fallback = decide("ollama", request, profile, None)
    assert fallback.tokens == 32768
    assert fallback.reason.startswith("Fit to GPU not computed; trained context 262144 capped")
    unknown = decide("llama_cpp", request, None, None, why="the GGUF is fetched by llama.cpp")
    assert unknown.tokens is None
    assert unknown.reason == "llama.cpp's own default: the GGUF is fetched by llama.cpp"


def test_the_catalog_declares_the_context_control_per_host() -> None:
    def control(facts: TargetFacts, service: str, pid: str):
        definition = next(row for row in service_definitions(facts) if row.id == service)
        rows = {row.id: row for row in definition.parameters}
        assert all(row.context_sizing is None for key, row in rows.items() if key != pid)
        return rows[pid].context_sizing

    gpu = control(_facts("nvidia"), "vllm", "max_model_len")
    assert gpu is not None and gpu.fit_to_gpu_available
    assert gpu.default_strategy == "fit_to_gpu" and gpu.strategies[0].id == "fit_to_gpu"
    assert gpu.choice_key == "context.choice" and gpu.choices == ["number", "max", "fit_to_gpu"]
    assert gpu.preview_path == "/v1/infrastructure/services/vllm/context-sizing"
    cpu = control(_facts("none"), "llama_cpp", "ctx_size")
    assert cpu is not None and not cpu.fit_to_gpu_available
    assert cpu.fit_to_gpu_reason == "No GPU was found on this host"
    ollama = control(_facts("none"), "ollama", "context_length")
    assert ollama is not None and ollama.fit_to_gpu_available


def test_controls_offer_max_always_and_fit_only_when_computable() -> None:
    request = SizingRequest(choice="fit_to_gpu", strategy="fit_to_gpu")
    profile = ModelMemoryProfile(262144, 147_456, source="config.json (qwen3)")
    budget = GpuBudget(36 * GIB, 10 * GIB)
    controls = deployment_controls("vllm", request, profile, budget)
    assert controls.semantics == "deployment"
    assert (controls.maximum, controls.maximum_reason) == (262144, "config.json (qwen3)")
    assert controls.fit_to_gpu.available and controls.fit_to_gpu.value == controls.current
    blind = deployment_controls("vllm", request, None, None, why="the model is a reference")
    assert blind.maximum is None and not blind.fit_to_gpu.available
    assert blind.fit_to_gpu.reason == "Fit to GPU cannot be computed: the model is a reference"
    installed = {
        EFFECTIVE_LENGTH: "65536",
        EFFECTIVE_REASON: "set by you (65536)",
        EFFECTIVE_CHOICE: "number",
    }
    running = deployment_controls("vllm", request, profile, budget, installed=installed)
    assert (running.current, running.current_choice) == (65536, "number")
