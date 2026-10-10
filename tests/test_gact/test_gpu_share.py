"""vLLM instances share a target's GPU only within its memory: their shares sum to at most 1.

An instance's share is ``context.gpu_share`` (else its typed memory
utilization, else vLLM's 0.9); a share chosen through the context control is
also what the instance launches with. A launch over the budget is refused with
the typed ``gpu_share_exceeded`` error, before it is queued and again under the
target lock.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from clio_agent.gact.infrastructure.drivers import build_driver_plan
from clio_agent.gact.infrastructure.gpu_share import (
    UTILIZATION_KEY,
    GpuShareExceededError,
    check_gpu_share,
    gpu_share,
)
from clio_agent.gact.infrastructure.models import (
    InfrastructureTarget,
    ServiceActionRequest,
    ServiceRecord,
    TargetFacts,
)
from clio_agent.gact.infrastructure.runtime import InfrastructureRuntime
from clio_agent.gact.infrastructure.store import InfrastructureStore


def _record(
    service_id: str, share: str = "", state: str = "running", variant: str = "native-cuda"
) -> ServiceRecord:
    configuration = {"model": "/data/models/q"}
    if share:
        configuration["context.gpu_share"] = share
    return ServiceRecord(
        id=f"local:{service_id}",
        service_id=service_id,
        target_id="local",
        variant_id=variant,
        configuration=configuration,
        state=state,  # type: ignore[arg-type]
    )


def test_the_share_of_a_deployment() -> None:
    assert gpu_share("cuda", {"context.gpu_share": "0.45"}) == 0.45
    assert gpu_share("native-cuda", {UTILIZATION_KEY: "0.3"}) == 0.3
    assert gpu_share("cuda", {"context.gpu_share": "0.4", UTILIZATION_KEY: "0.3"}) == 0.4
    assert gpu_share("rocm", {}) == 0.9  # vLLM's own default
    assert gpu_share("cpu", {"context.gpu_share": "0.5"}) is None


def test_two_instances_at_045_fit_one_gpu(tmp_path: Path) -> None:
    store = InfrastructureStore(tmp_path / "infra.json")
    store.put_service(_record("vllm@qwen3-4b", "0.45"))

    check_gpu_share(
        store, "local", "vllm@qwen3-1-7b", "start", "native-cuda", {"context.gpu_share": "0.45"}
    )
    check_gpu_share(  # 0.45 + 0.55 is exactly the whole GPU
        store, "local", "vllm@qwen3-1-7b", "start", "native-cuda", {"context.gpu_share": "0.55"}
    )


def test_a_launch_over_the_budget_is_refused_typed(tmp_path: Path) -> None:
    store = InfrastructureStore(tmp_path / "infra.json")
    store.put_service(_record("vllm@qwen3-4b", "0.45"))
    store.put_service(_record("vllm"))  # the default instance, at vLLM's 0.9

    with pytest.raises(GpuShareExceededError) as caught:
        check_gpu_share(
            store, "local", "vllm@small", "start", "native-cuda", {"context.gpu_share": "0.2"}
        )

    error = caught.value
    assert error.reason == "gpu_share_exceeded"
    assert str(error).startswith("gpu_share_exceeded:")
    assert error.details() == {
        "service_id": "vllm@small",
        "requested": 0.2,
        "running": {"vllm": 0.9, "vllm@qwen3-4b": 0.45},
        "total": pytest.approx(1.55),
    }


def test_stopped_cpu_and_other_targets_do_not_count(tmp_path: Path) -> None:
    store = InfrastructureStore(tmp_path / "infra.json")
    store.put_service(_record("vllm@stopped", "0.9", state="stopped"))
    store.put_service(_record("vllm@cpu", variant="cpu"))
    store.put_service(_record("vllm@elsewhere", "0.9").model_copy(update={"target_id": "other"}))
    store.put_service(_record("llama_cpp", "0.9"))

    share = {"context.gpu_share": "0.9"}
    check_gpu_share(store, "local", "vllm@new", "start", "native-cuda", share)


def test_only_a_launch_is_checked_and_a_start_reads_the_installed_share(tmp_path: Path) -> None:
    store = InfrastructureStore(tmp_path / "infra.json")
    store.put_service(_record("vllm@a", "0.6"))
    store.put_service(_record("vllm@b", "0.6", state="stopped"))

    # A native install only installs packages; a stop launches nothing.
    check_gpu_share(store, "local", "vllm@c", "install", "native-cuda", {"context.gpu_share": "1"})
    check_gpu_share(store, "local", "vllm@b", "stop", "", {})
    with pytest.raises(GpuShareExceededError):
        check_gpu_share(store, "local", "vllm@b", "start", "", {})  # its installed 0.6
    with pytest.raises(GpuShareExceededError):
        check_gpu_share(store, "local", "vllm@c", "install", "cuda", {"context.gpu_share": "0.5"})


def test_the_runtime_refuses_before_queueing(tmp_path: Path) -> None:
    store = InfrastructureStore(tmp_path / "infra.json")
    store.put_service(_record("vllm@a", "0.6"))
    runtime = InfrastructureRuntime(store, SimpleNamespace())  # type: ignore[arg-type]
    request = ServiceActionRequest(
        action="start", variant_id="native-cuda", configuration={"context.gpu_share": "0.5"}
    )

    with pytest.raises(GpuShareExceededError):
        runtime.start_action("vllm@b", request)
    assert store.operations() == []


def _native_install(**configuration: str) -> dict[str, object]:
    facts = TargetFacts.model_validate(
        {
            "target_id": "node",
            "label": "Node",
            "os": "linux",
            "arch": "x86_64",
            "accelerator": "nvidia",
            "uv_available": True,
            "hostname": "node",
            "agent_data_root": "/data/clio",
        }
    )
    plan = build_driver_plan(
        service_id="vllm@small",
        action="install",
        variant_id="native-cuda",
        configuration={"model": "/data/models/qwen3-1.7b", "port": "41001", **configuration},
        facts=facts,
        target=InfrastructureTarget(id="node", label="Node", kind="ssh"),
        api_key="per-launch",
    )
    install = json.loads(plan.commands[-1].stdin)
    return {"manifest": install["manifest"], "configuration": plan.configuration}


def test_the_chosen_share_is_the_launch_utilization_but_never_persisted() -> None:
    planned = _native_install(**{"context.gpu_share": "0.45"})

    arguments = planned["manifest"]["arguments"]  # type: ignore[index]
    flag = arguments.index("--gpu-memory-utilization")
    assert arguments[flag + 1] == "0.45"
    assert "--port" in arguments and arguments[arguments.index("--port") + 1] == "41001"
    configuration = planned["configuration"]
    assert isinstance(configuration, dict)
    assert configuration["context.gpu_share"] == "0.45"
    assert UTILIZATION_KEY not in configuration
    assert configuration["storage.service_directory"].endswith("clio-vllm-small")


def test_a_typed_utilization_wins_over_the_share() -> None:
    planned = _native_install(**{"context.gpu_share": "0.45", UTILIZATION_KEY: "0.4"})

    arguments = planned["manifest"]["arguments"]  # type: ignore[index]
    assert arguments[arguments.index("--gpu-memory-utilization") + 1] == "0.4"
    assert planned["configuration"][UTILIZATION_KEY] == "0.4"  # type: ignore[index]
