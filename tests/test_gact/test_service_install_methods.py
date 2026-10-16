"""Service methods qualify independently against the selected host."""

from __future__ import annotations

import pytest

from clio_agent.gact.infrastructure.drivers import build_driver_plan, service_definitions
from clio_agent.gact.infrastructure.models import ContainerRuntimeFact, TargetFacts


def facts(os: str, *, docker: bool = False, gpu: str = "none") -> TargetFacts:
    """Build explicit host facts; an installed but stopped engine remains unusable."""
    return TargetFacts(
        target_id="local" if os == "windows" else "site",
        label="Selected computer",
        os=os,
        arch="x86_64",
        accelerator=gpu,
        uv_available=True,
        home="C:\\Users\\alice" if os == "windows" else "/home/alice",
        container_runtimes=[ContainerRuntimeFact(name="docker", installed=True, usable=docker)],
    )


@pytest.mark.parametrize("service", ["vllm", "llama_cpp", "ollama"])
def test_windows_can_use_cpu_containers_independently_of_native_support(service: str) -> None:
    catalog = {row.id: row for row in service_definitions(facts("windows", docker=True))}
    cpu = next(row for row in catalog[service].variants if row.id == "cpu")
    assert cpu.compatible
    assert cpu.install_type == "container"
    assert "Docker" in cpu.reason


@pytest.mark.parametrize("gpu,native", [("nvidia", "native-cuda"), ("intel", "native-xpu")])
def test_site_without_containers_keeps_its_native_vllm_method(gpu: str, native: str) -> None:
    catalog = {row.id: row for row in service_definitions(facts("linux", gpu=gpu))}
    variants = {row.id: row for row in catalog["vllm"].variants}
    assert variants[native].compatible
    assert all(not row.compatible for row in variants.values() if row.install_type == "container")
    assert catalog["vllm"].recommended_variant == native
    for service in ("searxng", "model_router"):
        assert all(row.compatible for row in catalog[service].variants)
        assert all(row.install_type == "native_uv" for row in catalog[service].variants)


def test_windows_native_llama_remains_available_when_docker_is_stopped() -> None:
    catalog = {row.id: row for row in service_definitions(facts("windows"))}
    variants = {row.id: row for row in catalog["llama_cpp"].variants}
    assert variants["native-windows-cpu"].compatible
    assert not variants["cpu"].compatible
    assert catalog["llama_cpp"].recommended_variant == "native-windows-cpu"


def test_native_cpu_services_remain_usable_without_containers_or_a_gpu() -> None:
    catalog = {row.id: row for row in service_definitions(facts("linux"))}
    for service in ("llama_cpp", "ollama"):
        definition = catalog[service]
        selected = next(
            row for row in definition.variants if row.id == definition.recommended_variant
        )
        assert selected.id == "native-linux-cpu"
        assert selected.compatible
        assert selected.install_type == "native_uv"
    selected = next(
        row for row in catalog["vllm"].variants if row.id == catalog["vllm"].recommended_variant
    )
    assert selected.id == "cpu"
    assert "No container runtime" in selected.reason


def test_windows_ollama_cpu_plan_uses_powershell_readiness_and_no_posix_user_mapping() -> None:
    plan = build_driver_plan(
        service_id="ollama",
        action="install",
        variant_id="cpu",
        configuration={"model": "test", "container_runtime": "docker"},
        facts=facts("windows", docker=True),
    )
    launch = next(row for row in plan.commands if row.program == "docker" and row.args[0] == "run")
    assert "--user" not in launch.args
    assert "127.0.0.1:11434:11434" in launch.args
    assert plan.readiness is not None
    assert plan.readiness.health.program == "powershell"


def test_monitoring_dependency_requirement_is_explicit_and_no_native_support_is_fabricated() -> (
    None
):
    catalog = {row.id: row for row in service_definitions(facts("linux", docker=True))}
    for service in ("flowcept", "cmf"):
        variant = catalog[service].variants[0]
        assert variant.install_type == "supervised_stack"
        assert "container databases" in variant.label
