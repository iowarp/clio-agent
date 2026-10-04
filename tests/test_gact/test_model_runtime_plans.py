"""Managed model runtime plans: Ollama launcher, per-engine launch args, runtimes."""

from __future__ import annotations

import pytest
from clio_schemas.connected_resources import HostStorageLocations

from clio_agent.gact.infrastructure.container_runtime import RuntimeUnavailableError
from clio_agent.gact.infrastructure.drivers import (
    LOOPBACK_ONLY_SERVICES,
    build_driver_plan,
    service_connection_port,
    service_definitions,
)
from clio_agent.gact.infrastructure.model_runtimes import OLLAMA_VERSION, VLLM_VERSION
from clio_agent.gact.infrastructure.models import (
    CommandSpec,
    ContainerRuntimeFact,
    InfrastructureTarget,
    OwnedResource,
    TargetFacts,
    TargetIdentity,
)


def test_configured_storage_is_recorded_and_reused_after_host_defaults_change() -> None:
    facts = _facts("apptainer")
    target = InfrastructureTarget(
        id="ares", label="ares", kind="ssh", storage=HostStorageLocations(root="/data/clio")
    )
    installed = build_driver_plan(
        service_id="ollama",
        action="install",
        variant_id="cpu",
        configuration={"model": "tiny"},
        facts=facts,
        target=target,
    )
    assert installed.configuration["storage.model_cache"] == "/data/clio/models/ares/clio-ollama"
    assert installed.configuration["storage.temporary"] == "/data/clio/tmp/ares/clio-ollama"
    target.storage = HostStorageLocations(root="/other/new-default")
    started = build_driver_plan(
        service_id="ollama",
        action="start",
        variant_id="cpu",
        configuration=installed.configuration,
        facts=facts,
        target=target,
    )
    run = _run(started.commands, "apptainer")
    assert "/data/clio/models/ares/clio-ollama:/cache" in run.args
    assert not any("/other/new-default" in arg for arg in run.args)


def test_legacy_model_receipt_uses_recorded_cache_before_new_defaults() -> None:
    from clio_agent.gact.infrastructure.model_runtimes import deployment_storage_configuration

    target = InfrastructureTarget(id="ares", label="ares", kind="ssh", install_root="/old")
    old_cache = "/different/previous-root/services/ares/clio-ollama/cache"
    configuration = deployment_storage_configuration(
        "ollama",
        _facts("apptainer"),
        target,
        {"model": "tiny"},
        [OwnedResource(kind="directory", ref=old_cache)],
    )
    assert configuration["storage.model_cache"] == old_cache
    assert configuration["storage.service_directory"] == old_cache.removesuffix("/cache")


def _facts(*usable: str, os: str = "linux", target_id: str = "ares") -> TargetFacts:
    runtimes = [
        ContainerRuntimeFact(
            name=name,  # type: ignore[arg-type]
            installed=name in usable,
            usable=name in usable,
            reason=None if name in usable else "not_installed",
        )
        for name in ("docker", "podman", "apptainer")
    ]
    return TargetFacts(
        target_id=target_id,
        label="ares",
        os=os,
        arch="x86_64",
        docker_installed="docker" in usable,
        docker_available="docker" in usable,
        transport_state="connected",
        container_runtimes=runtimes,
        identity=TargetIdentity(uid=1008, gid=65534),
        home="/home/alice",
    )


def _run(plan_commands: tuple[CommandSpec, ...], program: str) -> CommandSpec:
    return next(
        spec
        for spec in plan_commands
        if spec.program == program
        and (spec.args[:1] == ["run"] or spec.args[:2] == ["instance", "run"])
    )


@pytest.mark.parametrize("runtime", ["docker", "podman", "apptainer"])
def test_vllm_downloaded_model_is_mounted_read_only(runtime: str) -> None:
    plan = build_driver_plan(
        service_id="vllm",
        action="install",
        variant_id="cpu",
        configuration={"model": "/data/models/downloaded", "container_runtime": runtime},
        facts=_facts(runtime),
    )
    command = _run(plan.commands, runtime)
    assert "/data/models/downloaded:/models/downloaded:ro" in command.args
    assert command.args[command.args.index("--model") + 1] == "/models/downloaded"
    assert "/data/models/downloaded" not in plan.configuration.get("storage.model_cache", "")


def test_ollama_install_pulls_the_pinned_image_and_then_the_model() -> None:
    plan = build_driver_plan(
        service_id="ollama",
        action="install",
        variant_id="cpu",
        configuration={
            "model": "qwen2.5:0.5b",
            "param.num_parallel": "2",
            "param.context_length": "4096",
        },
        facts=_facts("docker"),
    )

    image = f"ollama/ollama:{OLLAMA_VERSION}"
    assert (
        CommandSpec(program="docker", args=["pull", image], timeout_seconds=1800) in plan.commands
    )
    run = _run(plan.commands, "docker")
    args = run.args
    assert args[args.index("--publish") + 1] == "127.0.0.1:11434:11434"
    assert args[args.index("--user") + 1] == "1008:65534"
    assert "OLLAMA_HOST=0.0.0.0:11434" in args
    assert "OLLAMA_MODELS=/cache/models" in args
    assert "OLLAMA_NUM_PARALLEL=2" in args
    assert "OLLAMA_CONTEXT_LENGTH=4096" in args
    assert args[args.index("--volume") + 1] == (
        "/home/alice/.local/share/clio-agent/services/ares/clio-ollama/cache:/cache"
    )
    assert args[-2:] == [image, "serve"]
    assert plan.readiness is not None
    assert "http://127.0.0.1:11434/api/version" in plan.readiness.health.args
    assert [spec.args for spec in plan.after_ready] == [
        [
            "exec",
            "clio-ollama",
            "env",
            "OLLAMA_HOST=127.0.0.1:11434",
            "ollama",
            "pull",
            "qwen2.5:0.5b",
        ]
    ]
    assert plan.connection_port == 11434
    assert plan.configuration is not None and plan.configuration["container_runtime"] == "docker"


def test_model_cache_uses_the_target_agent_data_override() -> None:
    facts = _facts("docker").model_copy(update={"agent_data_root": "/scratch/alice/agent-data"})
    plan = build_driver_plan(
        service_id="ollama",
        action="install",
        variant_id="cpu",
        configuration={"model": "qwen2.5:0.5b"},
        facts=facts,
    )
    args = _run(plan.commands, "docker").args
    assert args[args.index("--volume") + 1] == (
        "/scratch/alice/agent-data/services/ares/clio-ollama/cache:/cache"
    )


def test_model_cache_rejects_relative_agent_data_root() -> None:
    facts = _facts("docker").model_copy(update={"agent_data_root": "relative"})
    with pytest.raises(ValueError, match="must be absolute"):
        build_driver_plan(
            service_id="ollama", action="install", variant_id="cpu", configuration={}, facts=facts
        )


def test_ollama_requires_a_model_to_pull() -> None:
    with pytest.raises(ValueError, match="model is required"):
        build_driver_plan(
            service_id="ollama",
            action="install",
            variant_id="cpu",
            configuration={},
            facts=_facts("docker"),
        )


def test_rootless_podman_runs_without_user_mapping_or_restart_policy() -> None:
    plan = build_driver_plan(
        service_id="llama_cpp",
        action="install",
        variant_id="cpu",
        configuration={
            "hf_model": "Qwen/Qwen2.5-0.5B-Instruct-GGUF:Q4_K_M",
            "container_runtime": "podman",
            "param.parallel": "2",
            "param.ctx_size": "4096",
            "param.threads": "4",
        },
        facts=_facts("docker", "podman"),
    )

    run = _run(plan.commands, "podman")
    assert "--user" not in run.args
    assert "--restart" not in run.args
    tail = run.args[run.args.index("-hf") :]
    assert tail[:6] == [
        "-hf",
        "Qwen/Qwen2.5-0.5B-Instruct-GGUF:Q4_K_M",
        "--host",
        "0.0.0.0",
        "--port",
        "8088",
    ]
    assert run.args[run.args.index("--parallel") + 1] == "2"
    assert run.args[run.args.index("--threads") + 1] == "4"
    assert "LLAMA_CACHE=/cache/llama.cpp" in run.args
    assert plan.configuration is not None and plan.configuration["container_runtime"] == "podman"


def test_llama_cpp_needs_exactly_one_model_source() -> None:
    for configuration in ({}, {"model_path": "/m.gguf", "hf_model": "a/b"}):
        with pytest.raises(ValueError, match="either a GGUF model path"):
            build_driver_plan(
                service_id="llama_cpp",
                action="install",
                variant_id="cpu",
                configuration=configuration,
                facts=_facts("docker"),
            )


def test_llama_cpp_mounts_a_gguf_path_read_only() -> None:
    plan = build_driver_plan(
        service_id="llama_cpp",
        action="install",
        variant_id="cpu",
        configuration={"model_path": "/scratch/qwen.gguf"},
        facts=_facts("docker"),
    )

    run = _run(plan.commands, "docker")
    assert "/scratch/qwen.gguf:/models/model.gguf:ro" in run.args
    assert run.args[run.args.index("-m") + 1] == "/models/model.gguf"


def test_vllm_cpu_launch_carries_parallelism_flags_and_cpu_environment() -> None:
    plan = build_driver_plan(
        service_id="vllm",
        action="install",
        variant_id="cpu",
        configuration={
            "model": "Qwen/Qwen2.5-0.5B-Instruct",
            "param.max_num_seqs": "4",
            "param.max_model_len": "4096",
            "param.tensor_parallel_size": "1",
            "param.cpu_kvcache_space": "2",
        },
        facts=_facts("docker"),
    )

    run = _run(plan.commands, "docker")
    assert f"vllm/vllm-openai-cpu:v{VLLM_VERSION}" in run.args
    tail = run.args[run.args.index(f"vllm/vllm-openai-cpu:v{VLLM_VERSION}") + 1 :]
    assert tail[:6] == [
        "--model",
        "Qwen/Qwen2.5-0.5B-Instruct",
        "--host",
        "0.0.0.0",
        "--port",
        "8000",
    ]
    assert tail[tail.index("--max-num-seqs") + 1] == "4"
    assert tail[tail.index("--max-model-len") + 1] == "4096"
    assert "VLLM_CPU_KVCACHE_SPACE=2" in run.args
    assert "HF_HOME=/cache/huggingface" in run.args
    assert run.args[run.args.index("--publish") + 1] == "127.0.0.1:8000:8000"


def test_apptainer_runs_an_instance_on_the_loopback_with_a_clio_owned_image_cache() -> None:
    plan = build_driver_plan(
        service_id="ollama",
        action="install",
        variant_id="cpu",
        configuration={"model": "qwen2.5:0.5b", "port": "21434"},
        facts=_facts("apptainer"),
    )

    service_dir = "/home/alice/.local/share/clio-agent/services/ares/clio-ollama"
    pull = next(spec for spec in plan.commands if spec.program == "env")
    assert pull.args == [
        f"APPTAINER_CACHEDIR={service_dir}/tmp/apptainer-cache",
        f"APPTAINER_TMPDIR={service_dir}/tmp/apptainer-cache",
        "apptainer",
        "pull",
        "--force",
        f"{service_dir}/images/clio-ollama.sif",
        f"docker://ollama/ollama:{OLLAMA_VERSION}",
    ]
    run = _run(plan.commands, "apptainer")
    assert run.args[:4] == ["instance", "run", "--cleanenv", "--writable-tmpfs"]
    assert "OLLAMA_HOST=127.0.0.1:21434" in run.args
    assert "HOME=/cache" not in run.args
    assert run.args[run.args.index("--home") + 1] == f"{service_dir}/cache:/cache"
    assert run.args[-3:] == [f"{service_dir}/images/clio-ollama.sif", "clio-ollama", "serve"]
    assert plan.connection_port == 21434


def test_install_stops_clios_own_leftover_then_refuses_a_busy_port() -> None:
    plan = build_driver_plan(
        service_id="vllm",
        action="install",
        variant_id="cpu",
        configuration={"model": "Qwen/Qwen2.5-0.5B-Instruct"},
        facts=_facts("docker"),
    )

    assert plan.commands[0].args == ["rm", "--force", "clio-vllm"]
    port_check = plan.commands[1]
    assert port_check.program == "sh" and "port_in_use" in port_check.args[1]
    assert port_check.args[-1] == "8000"


def test_no_usable_runtime_is_a_typed_refusal_and_an_incompatible_variant() -> None:
    facts = _facts()

    with pytest.raises(RuntimeUnavailableError) as raised:
        build_driver_plan(
            service_id="ollama",
            action="install",
            variant_id="cpu",
            configuration={"model": "qwen2.5:0.5b"},
            facts=facts,
        )
    assert raised.value.reason == "no_usable_runtime"
    ollama = next(row for row in service_definitions(facts) if row.id == "ollama")
    cpu = next(variant for variant in ollama.variants if variant.id == "cpu")
    assert not cpu.compatible
    assert "No container runtime can run services here" in cpu.reason


def test_catalog_declares_parameters_runtime_choices_and_port() -> None:
    definitions = {row.id: row for row in service_definitions(_facts("docker", "podman"))}

    for service_id in ("vllm", "llama_cpp", "ollama"):
        definition = definitions[service_id]
        fields = {field.id: field for field in definition.configuration_fields}
        assert fields["container_runtime"].options == ["docker", "podman"]
        assert fields["container_runtime"].placeholder == "Automatic (Docker)"
        assert "port" in fields
        assert definition.parameters, service_id
        assert service_id in LOOPBACK_ONLY_SERVICES
    assert {row.id for row in definitions["llama_cpp"].parameters} == {
        "parallel",
        "ctx_size",
        "threads",
    }
    assert {row.id for row in definitions["ollama"].parameters} == {
        "num_parallel",
        "context_length",
    }


def test_lifecycle_actions_use_the_installed_runtime() -> None:
    facts = _facts("docker", "podman")
    configuration = {"model": "qwen2.5:0.5b", "container_runtime": "podman"}
    for action, expected in (
        ("status", ["inspect", "--format", "{{.State.Status}}", "clio-ollama"]),
        ("stop", ["stop", "clio-ollama"]),
        ("start", ["start", "clio-ollama"]),
        ("logs", ["logs", "--tail", "80", "clio-ollama"]),
    ):
        plan = build_driver_plan(
            service_id="ollama",
            action=action,
            variant_id="cpu",
            configuration=configuration,
            facts=facts,
        )
        assert plan.commands[0].program == "podman"
        assert plan.commands[0].args == expected


def test_uninstall_removes_exactly_the_ledger_running_things_first() -> None:
    owned = [
        OwnedResource(kind="parent_directory", ref="/home/alice/.local/share/clio"),
        OwnedResource(
            kind="directory", ref="/home/alice/.local/share/clio/services/clio-ollama/cache"
        ),
        OwnedResource(kind="image", ref=f"ollama/ollama:{OLLAMA_VERSION}", runtime="docker"),
        OwnedResource(kind="container", ref="clio-ollama", runtime="docker"),
    ]
    plan = build_driver_plan(
        service_id="ollama",
        action="uninstall",
        variant_id="cpu",
        configuration={"model": "qwen2.5:0.5b", "container_runtime": "docker"},
        facts=_facts("docker"),
        owned=owned,
    )

    steps = [(spec.args[1].split(" ")[:3], spec.args[2:]) for spec in plan.commands]
    assert steps == [
        (["docker", "rm", "--force"], ["clio-ollama"]),
        (["docker", "rmi", '"$0"'], [f"ollama/ollama:{OLLAMA_VERSION}"]),
        (["rm", "-rf", "--"], ["/home/alice/.local/share/clio/services/clio-ollama/cache"]),
        (["rmdir", "--", '"$0"'], ["/home/alice/.local/share/clio"]),
    ]
    # Every removal confirms the thing is gone rather than trusting an exit code.
    assert all("still present" in spec.args[1] for spec in plan.commands[:3])


def test_uninstall_works_from_the_ledger_after_the_runtime_became_unusable() -> None:
    plan = build_driver_plan(
        service_id="ollama",
        action="uninstall",
        variant_id="cpu",
        configuration={"container_runtime": "podman"},
        facts=_facts("docker"),  # podman no longer usable
        owned=[OwnedResource(kind="container", ref="clio-ollama", runtime="podman")],
    )

    assert plan.commands[0].args[1].startswith("podman rm --force")


def test_reinstall_replaces_the_server_but_keeps_the_image_and_models() -> None:
    owned = [
        OwnedResource(
            kind="directory", ref="/home/alice/.local/share/clio/services/ares/clio-ollama/cache"
        ),
        OwnedResource(kind="image", ref=f"ollama/ollama:{OLLAMA_VERSION}", runtime="docker"),
        OwnedResource(kind="container", ref="clio-ollama", runtime="docker"),
    ]
    plan = build_driver_plan(
        service_id="ollama",
        action="reinstall",
        variant_id="cpu",
        configuration={"model": "qwen2.5:0.5b"},
        facts=_facts("docker"),
        owned=owned,
    )

    scripts = " ".join(" ".join(spec.args) for spec in plan.commands)
    assert "rm --force" in scripts
    assert "rmi" not in scripts
    assert "rm -rf" not in scripts


def test_a_mount_path_that_would_change_the_bind_is_refused() -> None:
    for path in ("/data/a:b.gguf", "/data/a,b.gguf"):
        with pytest.raises(ValueError, match="bind-mounted"):
            build_driver_plan(
                service_id="llama_cpp",
                action="install",
                variant_id="cpu",
                configuration={"model_path": path},
                facts=_facts("docker"),
            )


def test_rootless_docker_is_not_given_a_user_mapping() -> None:
    facts = _facts("docker")
    facts.container_runtimes[0].rootless = True
    plan = build_driver_plan(
        service_id="ollama",
        action="install",
        variant_id="cpu",
        configuration={"model": "qwen2.5:0.5b"},
        facts=facts,
    )

    assert "--user" not in _run(plan.commands, "docker").args


def test_configurable_port_is_validated_and_used_for_the_connection() -> None:
    assert service_connection_port("ollama", {"port": "21434"}) == 21434
    assert service_connection_port("llama_cpp", {}) == 8088
    with pytest.raises(ValueError, match="port must be"):
        service_connection_port("vllm", {"port": "80"})


def test_native_windows_llama_passes_server_parameters_to_the_process() -> None:
    from clio_agent.gact.infrastructure.store import InfrastructureStore  # noqa: PLC0415

    local = InfrastructureStore(None).target("local")
    assert local is not None
    plan = build_driver_plan(
        service_id="llama_cpp",
        action="start",
        variant_id="native-windows-cpu",
        configuration={"model_path": "C:/models/q.gguf", "param.parallel": "3"},
        facts=_facts("docker", os="windows", target_id="local"),
        target=local,
    )

    script = plan.commands[-1].args[-1]
    # Values are PowerShell literals inside the script, never trailing args
    # (-Command does not bind them to $args).
    assert plan.commands[-1].args[:3] == ["-NoProfile", "-NonInteractive", "-Command"]
    assert (
        "@('-m','C:/models/q.gguf','--host','127.0.0.1','--port','8088','--parallel','3')" in script
    )
    assert "$args" not in script
