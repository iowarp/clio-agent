"""Runtime-only installation and subsequent model launch are independent actions."""

from __future__ import annotations

import pytest

from clio_agent.gact.infrastructure.drivers import build_driver_plan
from clio_agent.gact.infrastructure.model_runtime_installation import llama_start_configuration
from clio_agent.gact.infrastructure.models import InfrastructureTarget
from tests.test_gact.test_model_runtime_plans import _facts, _run


@pytest.mark.parametrize("action", ["install", "reinstall"])
@pytest.mark.parametrize("runtime", ["docker", "podman", "apptainer"])
def test_installation_prepares_image_without_a_model_or_server(action: str, runtime: str) -> None:
    """Package installation neither sizes a model nor launches a server/container."""
    plan = build_driver_plan(
        service_id="llama_cpp",
        action=action,
        variant_id="cpu",
        configuration={"container_runtime": runtime},
        facts=_facts(runtime),
    )
    assert plan.connection_port is None and plan.readiness is None
    assert plan.before_launch is None and not plan.after_ready
    assert not any(
        command.args[:1] == ["run"] or command.args[:2] == ["instance", "run"]
        for command in plan.commands
    )
    assert not any(
        "--model" in command.args or "-m" in command.args or "-hf" in command.args
        for command in plan.commands
    )
    assert any(
        "pull" in command.args or any("pull " in arg for arg in command.args)
        for command in plan.commands
    )
    assert '"installed":true' in plan.commands[-1].args[-1]
    assert '"running":false' in plan.commands[-1].args[-1]
    assert plan.commands[-2].allowed_exit_codes == [0]


@pytest.mark.parametrize("action", ["install", "reinstall"])
def test_native_windows_installation_requires_no_gguf(action: str) -> None:
    """Windows install runs only the pinned archive's version check, never inference."""
    target = InfrastructureTarget(
        id="local", label="local", kind="local", install_root="C:/owned/runtime"
    )
    plan = build_driver_plan(
        service_id="llama_cpp",
        action=action,
        variant_id="native-windows-cpu",
        configuration={},
        facts=_facts(os="windows", target_id="local"),
        target=target,
    )
    scripts = "\n".join(command.args[-1] for command in plan.commands)
    assert "--version" in scripts and "SHA256]::Create" in scripts
    assert "c17f1e3233fc5f5b8915472affa939adee0c95785882503b106d5b14aba01002" in scripts
    assert "Start-Process" not in scripts and "server.pid') -Value" not in scripts
    assert plan.connection_port is None and plan.readiness is None and plan.before_launch is None
    assert '"installed":true' in scripts and '"serving":false' in scripts


@pytest.mark.parametrize("runtime", ["docker", "podman", "apptainer"])
def test_start_after_package_install_creates_server_with_chosen_model(runtime: str) -> None:
    """A prepared image has no stopped container for docker start to resume."""
    installed = build_driver_plan(
        service_id="llama_cpp",
        action="install",
        variant_id="cpu",
        configuration={"container_runtime": runtime},
        facts=_facts(runtime),
    )
    configuration = llama_start_configuration(
        installed.configuration or {}, {"hf_model": "org/model:Q4_K_M"}
    )
    started = build_driver_plan(
        service_id="llama_cpp",
        action="start",
        variant_id="cpu",
        configuration=configuration,
        facts=_facts(runtime),
    )
    launch = _run(started.commands, runtime)
    assert "-hf" in launch.args and "org/model:Q4_K_M" in launch.args
    assert started.connection_port == 8088 and started.readiness is not None
    assert '"serving":true' in started.after_ready[-1].args[-1]
    assert started.recorders


def test_start_applies_model_edits_and_explicit_clears_but_preserves_installation() -> None:
    """Changing the model source must not silently relocate runtime-owned files."""
    original = {
        "model_path": "/old.gguf",
        "container_runtime": "docker",
        "storage.model_cache": "/owned/cache",
    }
    changed = llama_start_configuration(original, {"model_path": "", "hf_model": "org/new:Q4"})
    assert changed["model_path"] == "" and changed["hf_model"] == "org/new:Q4"
    assert changed["storage.model_cache"] == "/owned/cache"
    assert original["model_path"] == "/old.gguf"
    for field, value in [("container_runtime", "podman"), ("storage.model_cache", "/elsewhere")]:
        with pytest.raises(ValueError, match="requires reconfiguration"):
            llama_start_configuration(original, {field: value})


def test_start_requires_a_model_on_native_windows() -> None:
    """Missing model validation happens before any server is stopped or started."""
    target = InfrastructureTarget(id="local", label="local", kind="local")
    with pytest.raises(ValueError, match="model_path is required"):
        build_driver_plan(
            service_id="llama_cpp",
            action="start",
            variant_id="native-windows-cpu",
            configuration={},
            facts=_facts(os="windows", target_id="local"),
            target=target,
        )
