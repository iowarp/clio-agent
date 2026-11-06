"""A target's GPUs are listed in its facts (``facts.gpus``), locally and over SSH alike.

Each vendor tool is read best effort: a missing tool or an unreadable answer is
an empty list, never a failed inspection. The catalog route exposes the list,
which is what a person pins deployments to.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from clio_agent.gact.infrastructure import gpu_inventory
from clio_agent.gact.infrastructure.gpu_inventory import (
    POSIX_GPU_PROBE,
    local_gpus,
    parse_gpu_lines,
    parse_nvidia,
    parse_rocm,
    parse_xpu,
)
from clio_agent.gact.infrastructure.models import (
    CommandResult,
    CommandSpec,
    GpuFact,
    InfrastructureTarget,
    SshRoute,
    TargetFacts,
)
from clio_agent.gact.infrastructure.probe import probe_target
from clio_agent.gact.routes.infrastructure import register_infrastructure_routes

#: ``nvidia-smi --query-gpu=index,name,memory.total,uuid --format=csv,noheader,nounits``
#: on a Polaris node (4 x A100-SXM4-40GB).
POLARIS_NVIDIA_SMI = "\n".join(
    f"{index}, NVIDIA A100-SXM4-40GB, 40960, GPU-{index}aaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    for index in range(4)
)


def test_nvidia_smi_lines_become_gpus_with_index_memory_and_uuid() -> None:
    gpus = parse_nvidia(POLARIS_NVIDIA_SMI + "\n")

    assert [gpu.index for gpu in gpus] == [0, 1, 2, 3]
    assert gpus[1] == GpuFact(
        index=1,
        name="NVIDIA A100-SXM4-40GB",
        vendor="nvidia",
        memory_total_mib=40960,
        uuid="GPU-1aaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
    )


def test_a_name_with_commas_and_unreported_memory_still_parse() -> None:
    gpus = parse_nvidia("0, Tesla, Inc. Board, [N/A], GPU-x\nNo devices were found\n")

    assert gpus == [
        GpuFact(
            index=0, name="Tesla, Inc. Board", vendor="nvidia", memory_total_mib=None, uuid="GPU-x"
        )
    ]


def test_rocm_smi_json_becomes_gpus_by_card_number() -> None:
    answer = {
        "card1": {
            "Card Series": "AMD Instinct MI250X",
            "VRAM Total Memory (B)": str(64 * 1024**3),
            "VRAM Total Used Memory (B)": "1024",
            "Unique ID": "0x18f68e602b8a790f",
        },
        "card0": {"Card Series": "AMD Instinct MI250X", "VRAM Total Memory (B)": "N/A"},
        "system": {"Driver version": "6.8"},
    }

    gpus = parse_rocm(json.dumps(answer))

    assert [(gpu.index, gpu.memory_total_mib) for gpu in gpus] == [(0, None), (1, 65536)]
    assert gpus[1].vendor == "amd" and gpus[1].uuid == "0x18f68e602b8a790f"
    assert gpus[1].name == "AMD Instinct MI250X"


def test_xpu_smi_discovery_becomes_gpus() -> None:
    answer = {
        "device_list": [
            {"device_id": 1, "device_name": "Intel(R) Data Center GPU Max 1550", "uuid": "u1"},
            {
                "device_id": 0,
                "device_name": "Intel(R) Data Center GPU Max 1550",
                "uuid": "u0",
                "memory_physical_size_byte": str(128 * 1024**3),
            },
        ]
    }

    gpus = parse_xpu(json.dumps(answer))

    assert [(gpu.index, gpu.vendor, gpu.memory_total_mib) for gpu in gpus] == [
        (0, "intel", 131072),
        (1, "intel", None),
    ]


@pytest.mark.parametrize("text", ["", "not json", "[]", '{"device_list": "x"}'])
def test_unreadable_answers_are_no_gpus(text: str) -> None:
    assert parse_rocm(text) == [] and parse_xpu(text) == [] and parse_nvidia(text) == []


def test_inspection_lines_are_read_by_their_vendor_tag() -> None:
    lines = [
        "Linux|x86_64|nvidia|1|1|1",
        *(f"clio-gpu-nvidia|{line}" for line in POLARIS_NVIDIA_SMI.splitlines()),
        "clio-gpu-unknown|0, Something, 1, GPU-y",
        "clio-agent-data|/home/alice/.local/share/clio-agent",
    ]

    assert [gpu.index for gpu in parse_gpu_lines(lines)] == [0, 1, 2, 3]


def test_the_posix_probe_prints_tagged_nvidia_lines(tmp_path: Path) -> None:
    """The shell snippet SSH inspection runs, against a stand-in nvidia-smi."""

    tool = tmp_path / "nvidia-smi"
    tool.write_text(f"#!/bin/sh\ncat <<'EOF'\n{POLARIS_NVIDIA_SMI}\nEOF\n", encoding="utf-8")
    tool.chmod(0o755)
    done = subprocess.run(
        ["sh", "-c", "gpu=nvidia; " + POSIX_GPU_PROBE],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
        env={**os.environ, "PATH": f"{tmp_path}{os.pathsep}{os.environ.get('PATH', '')}"},
    )

    assert [gpu.index for gpu in parse_gpu_lines(done.stdout.splitlines())] == [0, 1, 2, 3]


def test_the_posix_probe_prints_nothing_without_a_gpu() -> None:
    done = subprocess.run(
        ["sh", "-c", "gpu=none; " + POSIX_GPU_PROBE],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    assert done.stdout == ""


def test_an_ssh_target_lists_its_gpus_in_its_facts() -> None:
    target = InfrastructureTarget(
        id="polaris",
        label="Polaris",
        kind="ssh",
        transport_state="connected",
        ssh=SshRoute(host="x3006c0s1b0n0", platform="linux"),
    )
    asked: list[CommandSpec] = []

    async def execute(spec: CommandSpec) -> CommandResult:
        asked.append(spec)
        gpu_lines = "".join(f"clio-gpu-nvidia|{line}\n" for line in POLARIS_NVIDIA_SMI.splitlines())
        return CommandResult(
            exit_code=0,
            stdout="Linux|x86_64|nvidia|0|0|1\n" + gpu_lines + "clio-agent-data|/home/a/data\n",
        )

    facts = asyncio.run(probe_target(target, execute))

    assert "--query-gpu=index,name,memory.total,uuid" in asked[0].args[-1]
    assert facts.accelerator == "nvidia"
    assert [(gpu.index, gpu.memory_total_mib) for gpu in facts.gpus] == [
        (0, 40960),
        (1, 40960),
        (2, 40960),
        (3, 40960),
    ]


def test_an_ssh_target_without_gpu_lines_has_none() -> None:
    target = InfrastructureTarget(
        id="cpu", label="CPU", kind="ssh", transport_state="connected", ssh=SshRoute(host="h")
    )

    async def execute(spec: CommandSpec) -> CommandResult:
        return CommandResult(exit_code=0, stdout="Linux|x86_64|none|0|0|1\n")

    assert asyncio.run(probe_target(target, execute)).gpus == []


def test_local_gpus_run_the_vendor_tool_or_report_none(monkeypatch: pytest.MonkeyPatch) -> None:
    ran: list[list[str]] = []

    def run(command: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        ran.append(command)
        return subprocess.CompletedProcess(command, 0, POLARIS_NVIDIA_SMI, "")

    monkeypatch.setattr(gpu_inventory.subprocess, "run", run)

    assert len(local_gpus("nvidia", lambda name: f"/usr/bin/{name}")) == 4
    assert ran[0][1:] == gpu_inventory.NVIDIA_QUERY[1:]
    assert local_gpus("nvidia", lambda _name: None) == []  # no nvidia-smi on PATH
    assert local_gpus("none", lambda name: f"/usr/bin/{name}") == []


def test_the_catalog_exposes_the_gpus(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from clio_agent.gact.infrastructure import runtime as runtime_module

    async def facts(target: InfrastructureTarget, execute: Any = None) -> TargetFacts:
        return TargetFacts(
            target_id=target.id,
            label=target.label,
            os="linux",
            arch="x86_64",
            accelerator="nvidia",
            transport_state="connected",
            gpus=parse_nvidia(POLARIS_NVIDIA_SMI),
        )

    monkeypatch.setattr(runtime_module, "probe_target", facts)
    app = FastAPI()
    register_infrastructure_routes(app, tmp_path)
    with TestClient(app) as client:
        response = client.get("/v1/infrastructure/catalog", params={"target_id": "local"})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["facts"]["gpus"][2] == {
        "index": 2,
        "name": "NVIDIA A100-SXM4-40GB",
        "vendor": "nvidia",
        "memory_total_mib": 40960,
        "uuid": "GPU-2aaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
    }
    rows = {row["id"]: row for row in body["services"]}
    assert rows["vllm"]["supports_instances"] is True
    assert rows["llama_cpp"]["supports_instances"] is True
    assert rows["ollama"]["supports_instances"] is False
    # The last catalog's GPUs are what an action's pin is checked against before queueing.
    assert len(app.state.infrastructure_runtime._target_gpus["local"]) == 4  # noqa: SLF001
