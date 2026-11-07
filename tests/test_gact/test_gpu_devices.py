"""Manual GPU placement: each deployment's device pin, compiled per engine and checked typed.

A person pins a deployment to GPUs (``param.cuda_devices`` / ``param.rocm_devices``)
so several services share one node without sharing a GPU, or one vLLM spans
several. Malformed, unknown or (vLLM) miscounted pins are refused with
``gpu_device_invalid`` / ``gpu_device_unknown`` / ``gpu_device_count_mismatch``
-- before queueing (a 422) and again when the plan compiles.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from clio_agent.gact.infrastructure.container_runtime import ContainerLaunch, run_command
from clio_agent.gact.infrastructure.drivers import build_driver_plan
from clio_agent.gact.infrastructure.gpu_devices import (
    check_gpu_devices,
    fact_rows,
    select_gpu_rows,
)
from clio_agent.gact.infrastructure.models import (
    ContainerRuntimeFact,
    GpuFact,
    RuntimeName,
    ServiceActionRequest,
    TargetFacts,
    TargetIdentity,
)
from clio_agent.gact.infrastructure.runtime import InfrastructureRuntime
from clio_agent.gact.infrastructure.server_parameters import (
    GpuDeviceError,
    compile_parameters,
    engine_parameters,
)
from clio_agent.gact.infrastructure.store import InfrastructureStore
from clio_agent.gact.routes.infrastructure import register_infrastructure_routes

FOUR_A100 = [
    GpuFact(index=index, name="NVIDIA A100-SXM4-40GB", vendor="nvidia", memory_total_mib=40960)
    for index in range(4)
]


def _facts(gpus: list[GpuFact] | None = None, runtime: RuntimeName = "docker") -> TargetFacts:
    return TargetFacts(
        target_id="polaris",
        label="polaris",
        os="linux",
        arch="x86_64",
        accelerator="nvidia",
        docker_installed=True,
        docker_available=True,
        transport_state="connected",
        container_runtimes=[ContainerRuntimeFact(name=runtime, installed=True, usable=True)],
        identity=TargetIdentity(uid=1008, gid=65534),
        home="/home/alice",
        hostname="polaris",
        gpus=FOUR_A100 if gpus is None else gpus,
    )


def _reason(error: pytest.ExceptionInfo[GpuDeviceError]) -> str:
    return error.value.reason


# --------------------------------------------------------------------------- parameters


def test_a_cuda_pin_is_the_visible_devices_in_pci_order() -> None:
    compiled = compile_parameters("vllm", "cuda", {"param.cuda_devices": " 2, 3 "})

    assert dict(compiled.env) == {"CUDA_VISIBLE_DEVICES": "2,3", "CUDA_DEVICE_ORDER": "PCI_BUS_ID"}
    assert compiled.values["cuda_devices"] == "2,3"


@pytest.mark.parametrize("raw", ["a", "-1", "1,1", "1,,2", "1.5", "０"])
def test_a_malformed_pin_is_refused_typed(raw: str) -> None:
    with pytest.raises(GpuDeviceError) as error:
        compile_parameters("vllm", "cuda", {"param.cuda_devices": raw})
    assert _reason(error) == "gpu_device_invalid"
    assert str(error.value).startswith("gpu_device_invalid:")
    assert error.value.details()["parameter"] == "cuda_devices"


@pytest.mark.parametrize(
    ("engine", "variant", "pid", "variable"),
    [
        ("vllm", "rocm", "rocm_devices", "HIP_VISIBLE_DEVICES"),
        ("llama_cpp", "cuda", "cuda_devices", "CUDA_VISIBLE_DEVICES"),
        ("llama_cpp", "rocm", "rocm_devices", "HIP_VISIBLE_DEVICES"),
        ("ollama", "cuda", "cuda_devices", "CUDA_VISIBLE_DEVICES"),
        ("ollama", "rocm", "rocm_devices", "ROCR_VISIBLE_DEVICES"),
    ],
)
def test_each_engine_pins_with_its_vendor_variable(
    engine: str, variant: str, pid: str, variable: str
) -> None:
    compiled = compile_parameters(engine, variant, {f"param.{pid}": "1"})  # type: ignore[arg-type]

    assert dict(compiled.env)[variable] == "1"
    rows = engine_parameters(engine, variant)  # type: ignore[arg-type]
    declared = next(row for row in rows if row.id == pid)
    assert declared.value_format == "device_list"


@pytest.mark.parametrize(
    ("engine", "variant", "pid"),
    [
        ("vllm", "cpu", "cuda_devices"),
        ("vllm", "cuda", "rocm_devices"),
        ("llama_cpp", "cpu", "cuda_devices"),
        ("llama_cpp", "vulkan", "cuda_devices"),
        ("ollama", "cpu", "rocm_devices"),
    ],
)
def test_a_pin_for_another_backend_is_refused(engine: str, variant: str, pid: str) -> None:
    with pytest.raises(ValueError, match="does not apply"):
        compile_parameters(engine, variant, {f"param.{pid}": "0"})  # type: ignore[arg-type]


def test_llama_cpp_multi_gpu_split_flags() -> None:
    compiled = compile_parameters(
        "llama_cpp",
        "cuda",
        {
            "param.cuda_devices": "0,1",
            "param.split_mode": "row",
            "param.tensor_split": "3, 1",
            "param.main_gpu": "1",
        },
    )
    flags = list(compiled.flags)

    assert flags[flags.index("--split-mode") + 1] == "row"
    assert flags[flags.index("--tensor-split") + 1] == "3,1"
    assert flags[flags.index("--main-gpu") + 1] == "1"
    with pytest.raises(ValueError, match="must be one of"):
        compile_parameters("llama_cpp", "cuda", {"param.split_mode": "column"})
    for bad in ("a,b", "0,0", "-1,2"):
        with pytest.raises(ValueError, match="GPU split proportions"):
            compile_parameters("llama_cpp", "cuda", {"param.tensor_split": bad})
    with pytest.raises(ValueError, match="does not apply"):
        compile_parameters("llama_cpp", "cpu", {"param.split_mode": "none"})


# --------------------------------------------------------------------------- checks


def test_a_pin_must_name_gpus_the_host_lists() -> None:
    check_gpu_devices("vllm", {"param.cuda_devices": "3"}, FOUR_A100)
    with pytest.raises(GpuDeviceError) as error:
        check_gpu_devices("vllm", {"param.cuda_devices": "4"}, FOUR_A100)

    assert _reason(error) == "gpu_device_unknown"
    assert "GPU 4 is not on this host" in str(error.value)
    assert error.value.details()["available"] == [0, 1, 2, 3]
    check_gpu_devices("vllm", {"param.cuda_devices": "9"}, [])  # GPUs not known: not checked
    with pytest.raises(GpuDeviceError, match="lists no AMD GPU"):
        check_gpu_devices("ollama", {"param.rocm_devices": "0"}, FOUR_A100)


def test_vllm_needs_one_pinned_gpu_per_worker() -> None:
    with pytest.raises(GpuDeviceError) as error:
        check_gpu_devices(
            "vllm", {"param.cuda_devices": "1", "param.tensor_parallel_size": "2"}, FOUR_A100
        )

    assert _reason(error) == "gpu_device_count_mismatch"
    message = str(error.value)
    assert "tensor parallel size 2" in message and "1 GPU(s) are pinned" in message
    assert error.value.details()["devices"] == 1
    with pytest.raises(GpuDeviceError, match="gpu_device_count_mismatch"):
        check_gpu_devices("vllm", {"param.cuda_devices": "0,1"}, FOUR_A100)  # TP defaults to 1
    check_gpu_devices(
        "vllm", {"param.cuda_devices": "0,1", "param.tensor_parallel_size": "2"}, FOUR_A100
    )
    check_gpu_devices(
        "vllm",
        {
            "param.cuda_devices": "0,1,2,3",
            "param.tensor_parallel_size": "2",
            "param.pipeline_parallel_size": "2",
        },
        FOUR_A100,
    )
    # An Intel mask may name tiles xpu-smi does not list: only its count is checked.
    check_gpu_devices(
        "vllm", {"param.xpu_devices": "0.0,0.1", "param.tensor_parallel_size": "2"}, FOUR_A100
    )
    check_gpu_devices("llama_cpp", {"param.cuda_devices": "0,1"}, FOUR_A100)  # no TP rule
    check_gpu_devices("vllm", {"param.tensor_parallel_size": "4"}, FOUR_A100)  # unpinned


def test_llama_cpp_main_gpu_counts_among_the_pinned_devices() -> None:
    check_gpu_devices("llama_cpp", {"param.cuda_devices": "2,3", "param.main_gpu": "1"})
    with pytest.raises(GpuDeviceError) as error:
        check_gpu_devices("llama_cpp", {"param.cuda_devices": "2,3", "param.main_gpu": "2"})
    assert _reason(error) == "gpu_device_unknown"
    assert error.value.details()["parameter"] == "main_gpu"


def test_sizing_rows_follow_the_pin_in_host_order() -> None:
    rows = [{"total": 40, "free": 40 - index} for index in range(4)]

    assert select_gpu_rows({}, rows) == rows
    assert select_gpu_rows({"param.cuda_devices": "2,1"}, rows) == [rows[2], rows[1]]
    assert select_gpu_rows({"param.cuda_devices": "1,7"}, rows) == [rows[1]]
    assert select_gpu_rows({"param.xpu_devices": "0.1"}, rows) == rows
    assert [row["total"] for row in fact_rows(FOUR_A100)] == [40960 << 20] * 4
    gap = [FOUR_A100[0], FOUR_A100[2]]
    assert fact_rows(gap) == []  # positional rows need every index


# --------------------------------------------------------------------------- containers


def _launch(**overrides: object) -> ContainerLaunch:
    base: dict[str, object] = {
        "name": "clio-vllm-spotter",
        "image": "vllm/vllm-openai@sha256:x",
        "port": 41001,
        "container_port": 41001,
        "args": ("--model", "m"),
        "env": (("CUDA_VISIBLE_DEVICES", "1,2"), ("CUDA_DEVICE_ORDER", "PCI_BUS_ID")),
        "accelerator": "nvidia",
        "gpu_devices": (1, 2),
    }
    return ContainerLaunch(**{**base, **overrides})  # type: ignore[arg-type]


def test_docker_exposes_only_the_pinned_gpus() -> None:
    args = run_command("docker", _launch(), TargetIdentity(), "/images").args

    assert args[args.index("--gpus") + 1] == '"device=1,2"'
    assert "all" not in args
    assert "CUDA_VISIBLE_DEVICES=1,2" not in args  # renumbered from 0 inside
    assert "CUDA_DEVICE_ORDER=PCI_BUS_ID" in args


def test_podman_names_each_pinned_cdi_device() -> None:
    args = run_command("podman", _launch(), TargetIdentity(), "/images").args

    assert "--gpus" not in args
    devices = [args[i + 1] for i, arg in enumerate(args) if arg == "--device"]
    assert devices == ["nvidia.com/gpu=1", "nvidia.com/gpu=2"]


def test_apptainer_keeps_nv_and_the_host_numbered_pin() -> None:
    args = run_command("apptainer", _launch(), TargetIdentity(), "/images").args

    assert "--nv" in args
    assert "CUDA_VISIBLE_DEVICES=1,2" in args


def test_an_unpinned_container_keeps_every_gpu() -> None:
    args = run_command("docker", _launch(gpu_devices=(), env=()), TargetIdentity(), "/images").args

    assert args[args.index("--gpus") + 1] == "all"


def test_a_pinned_vllm_container_plan_maps_its_devices() -> None:
    plan = build_driver_plan(
        service_id="vllm@spotter",
        action="install",
        variant_id="cuda",
        configuration={
            "model": "Qwen/Qwen3-4B",
            "port": "41001",
            "param.cuda_devices": "1,2",
            "param.tensor_parallel_size": "2",
            "param.max_model_len": "8192",
        },
        facts=_facts(),
    )
    run = next(
        spec.args for spec in plan.commands if spec.program == "docker" and spec.args[:1] == ["run"]
    )

    assert run[run.index("--gpus") + 1] == '"device=1,2"'
    assert "--name" in run and run[run.index("--name") + 1] == "clio-vllm-spotter"


def test_the_plan_refuses_a_pin_the_host_does_not_have() -> None:
    with pytest.raises(GpuDeviceError) as error:
        build_driver_plan(
            service_id="llama_cpp@b",
            action="start",
            variant_id="cuda",
            configuration={
                "hf_model": "Qwen/Qwen2.5-0.5B-Instruct-GGUF:Q4_K_M",
                "port": "41002",
                "param.cuda_devices": "5",
            },
            facts=_facts(),
        )
    assert _reason(error) == "gpu_device_unknown"


# --------------------------------------------------------------------------- admission


def test_the_runtime_refuses_a_bad_pin_before_queueing(tmp_path: Path) -> None:
    store = InfrastructureStore(tmp_path / "infra.json")
    runtime = InfrastructureRuntime(store, SimpleNamespace())  # type: ignore[arg-type]
    runtime._target_gpus["local"] = FOUR_A100  # noqa: SLF001 - the last catalog's GPUs

    for configuration, reason in (
        ({"param.cuda_devices": "1,x"}, "gpu_device_invalid"),
        ({"param.cuda_devices": "6"}, "gpu_device_unknown"),
        (
            {"param.cuda_devices": "1", "param.tensor_parallel_size": "2"},
            "gpu_device_count_mismatch",
        ),
    ):
        request = ServiceActionRequest(
            action="install",
            variant_id="cuda",
            configuration={"model": "Qwen/Qwen3-4B", **configuration},
        )
        with pytest.raises(GpuDeviceError) as error:
            runtime.start_action("vllm@spotter", request)
        assert error.value.reason == reason
    assert store.operations() == []


def test_the_action_route_answers_a_typed_422(tmp_path: Path) -> None:
    app = FastAPI()
    register_infrastructure_routes(app, tmp_path)
    with TestClient(app) as client:
        response = client.post(
            "/v1/infrastructure/services/vllm@spotter/actions",
            json={
                "action": "install",
                "variant_id": "cuda",
                "configuration": {
                    "model": "Qwen/Qwen3-4B",
                    "param.cuda_devices": "1",
                    "param.tensor_parallel_size": "2",
                },
            },
        )

    assert response.status_code == 422, response.text
    error = response.json()["detail"]["error"]
    assert error["error"] == "gpu_device_count_mismatch"
    assert error["details"]["service_id"] == "vllm@spotter"
    assert error["details"]["tensor_parallel_size"] == 2
    assert app.state.infrastructure_store.operations() == []
