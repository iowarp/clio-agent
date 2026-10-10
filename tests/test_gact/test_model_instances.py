"""Several named vLLM instances on one target, beside the default deployment.

The engine's own service id (``vllm``) stays its default instance: existing
records, keys and saved servers keep working. ``vllm@<name>`` is a further
instance with its own container and service directory, its own loopback port
(chosen free on the target), its own key and its own ledger row.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from clio_agent.gact.infrastructure.drivers import (
    build_driver_plan,
    loopback_only,
    service_connection_port,
    service_definitions,
)
from clio_agent.gact.infrastructure.model_instances import (
    FREE_PORT_MARKER,
    ROUTER_SERVICE,
    InstanceNameError,
    allocate_port,
    engine_of,
    free_port_command,
    instance_container_name,
    instance_service_id,
    is_named_instance,
    named_instance_ids,
    needs_port,
    split_service_id,
    validate_service_id,
)
from clio_agent.gact.infrastructure.model_router_runtime import ModelRouterMixin
from clio_agent.gact.infrastructure.models import (
    CommandResult,
    CommandSpec,
    ContainerRuntimeFact,
    ServiceActionRequest,
    ServiceRecord,
    TargetFacts,
    TargetIdentity,
)
from clio_agent.gact.infrastructure.server_access import (
    deployment_key_ref,
    launch_key,
    managed_credential_ref,
    store_key,
)
from clio_agent.gact.infrastructure.store import InfrastructureStore


@pytest.fixture(autouse=True)
def _isolated_user_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLIO_USER_DIR", str(tmp_path / "user"))


def _facts() -> TargetFacts:
    return TargetFacts(
        target_id="ares",
        label="ares",
        os="linux",
        arch="x86_64",
        docker_installed=True,
        docker_available=True,
        transport_state="connected",
        container_runtimes=[ContainerRuntimeFact(name="docker", installed=True, usable=True)],
        identity=TargetIdentity(uid=1008, gid=65534),
        home="/home/alice",
        hostname="ares",
    )


def _record(service_id: str, port: str, state: str = "running", **extra: str) -> ServiceRecord:
    return ServiceRecord(
        id=f"local:{service_id}",
        service_id=service_id,
        target_id="local",
        variant_id="cpu",
        configuration={"model": "Qwen/Qwen3-4B", "port": port, **extra},
        state=state,  # type: ignore[arg-type]
        connection_url=f"http://127.0.0.1:{port}",
    )


# --------------------------------------------------------------------------- naming


def test_the_engine_id_is_its_default_instance() -> None:
    assert split_service_id("vllm") == ("vllm", "")
    assert split_service_id("vllm@small") == ("vllm", "small")
    assert engine_of("vllm@small") == "vllm"
    assert engine_of("flowcept") == "flowcept"
    assert not is_named_instance("vllm")
    assert is_named_instance("vllm@small")
    assert instance_service_id("vllm") == "vllm"
    assert instance_service_id("vllm", "qwen3-4b") == "vllm@qwen3-4b"


@pytest.mark.parametrize(
    "service_id",
    ["vllm@", "vllm@Small", "vllm@-a", "vllm@a_b", "vllm@a/b", "vllm@a\n", "ollama@x", "@x"],
)
def test_an_instance_id_clio_cannot_deploy_is_refused_typed(service_id: str) -> None:
    with pytest.raises(InstanceNameError) as caught:
        validate_service_id(service_id)
    assert caught.value.reason == "invalid_instance_name"


def test_instances_have_their_own_container_and_catalog_row() -> None:
    assert instance_container_name("clio-vllm", "vllm") == "clio-vllm"
    assert instance_container_name("clio-vllm", "vllm@small") == "clio-vllm-small"

    rows = {row.id: row for row in service_definitions(_facts(), ["vllm@small", "vllm"])}

    assert rows["vllm"].label == "vLLM"
    assert rows["vllm@small"].label == "vLLM (small)"
    assert rows["vllm@small"].variants == rows["vllm"].variants
    assert ROUTER_SERVICE in rows
    assert loopback_only("vllm@small") and loopback_only(ROUTER_SERVICE)


def test_the_catalog_lists_only_named_instances_recorded_on_the_target() -> None:
    records = [
        _record("vllm", "8000"),
        _record("vllm@small", "41001"),
        _record("vllm@big", "41002").model_copy(update={"target_id": "other"}),
    ]
    assert named_instance_ids(records, "local") == ["vllm@small"]


def test_an_existing_single_instance_record_loads_unchanged(tmp_path: Path) -> None:
    """Migration-free: a store written before instances existed reads the same."""

    path = tmp_path / "infra.json"
    legacy = _record("vllm", "8000").model_dump(mode="json")
    path.write_text(
        json.dumps({"schema_version": 1, "services": {"local:vllm": legacy}}), encoding="utf-8"
    )
    store = InfrastructureStore(path)

    record = store.service("local", "vllm")
    assert record is not None and record.service_id == "vllm"
    assert service_connection_port("vllm", record.configuration, record.variant_id) == 8000
    assert deployment_key_ref("local", "vllm") == "managed-server:local:vllm"


# --------------------------------------------------------------------------- plans


def _install(service_id: str, port: str) -> tuple[str, str]:
    plan = build_driver_plan(
        service_id=service_id,
        action="install",
        variant_id="cpu",
        configuration={"model": "Qwen/Qwen3-4B", "port": port},
        facts=_facts(),
        api_key="k-" + service_id,
    )
    commands = " ".join(" ".join([spec.program, *spec.args]) for spec in plan.commands)
    assert plan.configuration is not None
    return commands, plan.configuration["storage.service_directory"]


def test_each_instance_gets_its_own_container_directory_and_port() -> None:
    default, default_dir = _install("vllm", "8000")
    named, named_dir = _install("vllm@small", "41001")

    assert "--name clio-vllm " in default and "127.0.0.1:8000" in default
    assert "--name clio-vllm-small " in named and "41001" in named
    assert "clio-vllm " not in named
    assert default_dir.endswith("/services/ares/clio-vllm")
    assert named_dir.endswith("/services/ares/clio-vllm-small")
    assert "k-vllm@small" not in named  # the key never is an argument


def test_each_instance_has_its_own_key(tmp_path: Path) -> None:
    first = launch_key("local", "vllm@a", "install", {})
    second = launch_key("local", "vllm@b", "install", {})
    assert first and second and first != second
    store_key("local", "vllm@a", "stored-a")
    assert launch_key("local", "vllm@a", "start", {}) == "stored-a"
    assert deployment_key_ref("local", "vllm@a") != deployment_key_ref("local", "vllm")

    store = InfrastructureStore(tmp_path / "infra.json")
    store.put_service(_record("vllm@a", "41001"))
    url = "http://127.0.0.1:41001/v1"
    assert managed_credential_ref(store, "vllm", url) == "managed-server:local:vllm@a"


# --------------------------------------------------------------------------- ports


def test_named_instances_and_the_router_get_a_port_the_default_keeps_its_own() -> None:
    assert needs_port("vllm@small", "install", {})
    assert needs_port(ROUTER_SERVICE, "install", {"port": ""})
    assert not needs_port("vllm@small", "install", {"port": "41001"})
    assert not needs_port("vllm@small", "status", {})
    assert not needs_port("vllm", "install", {})


def test_the_free_port_script_asks_the_os_and_skips_taken_ports() -> None:
    spec = free_port_command([8000, 41001])
    assert spec.args[-2:] == ["8000", "41001"]
    done = subprocess.run(
        [sys.executable, *spec.args], capture_output=True, text=True, check=True, timeout=30
    )
    marker, port = done.stdout.split()
    assert marker == FREE_PORT_MARKER and int(port) >= 1024
    assert int(port) not in {8000, 41001}


def test_allocation_reads_the_target_answer_or_fails_typed() -> None:
    async def answers(spec: CommandSpec) -> CommandResult:
        assert spec.program == "python3" and "8000" in spec.args
        return CommandResult(exit_code=0, stdout=f"{FREE_PORT_MARKER} 41234\n")

    async def refuses(spec: CommandSpec) -> CommandResult:
        return CommandResult(exit_code=127, stderr="python3 is not installed")

    assert asyncio.run(allocate_port(answers, {8000})) == 41234
    with pytest.raises(ValueError, match="port_unavailable"):
        asyncio.run(allocate_port(refuses, set()))


class _Ports(ModelRouterMixin):
    def __init__(self, store: InfrastructureStore) -> None:
        self.store = store
        self.asked: list[CommandSpec] = []

    async def _execute(self, target_id: str, spec: CommandSpec) -> CommandResult:
        self.asked.append(spec)
        return CommandResult(exit_code=0, stdout=f"{FREE_PORT_MARKER} 41999\n")


def test_a_launch_excludes_every_port_recorded_on_the_target(tmp_path: Path) -> None:
    store = InfrastructureStore(tmp_path / "infra.json")
    store.put_service(_record("vllm", "8000"))
    store.put_service(_record("vllm@a", "41001"))
    runtime = _Ports(store)
    request = ServiceActionRequest(action="install", variant_id="cpu", configuration={})

    chosen = asyncio.run(runtime._prepare_launch("vllm@b", request))  # noqa: SLF001

    assert chosen.configuration["port"] == "41999"
    assert set(runtime.asked[0].args[2:]) == {"8000", "41001"}
    kept = request.model_copy(update={"configuration": {"port": "42000"}})
    assert asyncio.run(runtime._prepare_launch("vllm@b", kept)) == kept  # noqa: SLF001


def test_an_invalid_instance_is_refused_before_it_is_queued(tmp_path: Path) -> None:
    from clio_agent.gact.infrastructure.runtime import InfrastructureRuntime  # noqa: PLC0415

    store = InfrastructureStore(tmp_path / "infra.json")
    runtime = InfrastructureRuntime(store, SimpleNamespace())  # type: ignore[arg-type]

    with pytest.raises(InstanceNameError):
        runtime.start_action("vllm@Bad", ServiceActionRequest(action="install", variant_id="cpu"))
    assert store.operations() == []
