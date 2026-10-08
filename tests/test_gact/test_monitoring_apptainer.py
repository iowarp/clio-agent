"""Flowcept dependencies on Apptainer: plan gating, loopback configs and instance commands."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from clio_agent.gact.infrastructure import node_service_stack
from clio_agent.gact.infrastructure import node_service_stack_apptainer as backend
from clio_agent.gact.infrastructure.drivers import build_driver_plan, service_definitions
from clio_agent.gact.infrastructure.models import (
    ContainerRuntimeFact,
    InfrastructureTarget,
    TargetFacts,
)


def apptainer_facts() -> TargetFacts:
    return TargetFacts(
        target_id="node",
        label="Node",
        os="linux",
        arch="x86_64",
        accelerator="nvidia",
        uv_available=True,
        docker_available=False,
        agent_data_root="/data/clio",
        hostname="node",
        container_runtimes=[
            ContainerRuntimeFact(name="apptainer", installed=True, usable=True, version="1.5.3")
        ],
    )


def apptainer_manifest(service: str, **configuration: str) -> dict[str, Any]:
    plan = build_driver_plan(
        service_id=service,
        action="install",
        variant_id="managed",
        configuration=configuration,
        facts=apptainer_facts(),
        target=InfrastructureTarget(id="node", label="Node", kind="ssh"),
    )
    return json.loads(plan.commands[-1].stdin)["manifest"]


def test_apptainer_only_host_offers_flowcept_but_explains_cmf() -> None:
    rows = {row.id: row for row in service_definitions(apptainer_facts())}
    assert rows["flowcept"].variants[0].compatible
    assert rows["flowcept"].configuration_fields[0].options == ["apptainer"]
    assert rows["flowcept"].configuration_fields[1].placeholder == "service"
    assert not rows["cmf"].variants[0].compatible
    assert "Docker or Podman" in (rows["cmf"].variants[0].reason or "")


def test_apptainer_keeps_sifs_in_the_service_folder_and_ships_its_backend() -> None:
    manifest = apptainer_manifest("flowcept")
    assert manifest["container_runtime"] == "apptainer"
    assert manifest["image_storage"] == "service"
    assert "pod_infra_image" not in manifest
    assert "stack_apptainer.py" in manifest["files"]
    with pytest.raises(ValueError, match="service folder"):
        apptainer_manifest("flowcept", image_storage="engine")


def test_host_network_components_listen_on_loopback_private_ports() -> None:
    manifest = apptainer_manifest("flowcept", redis_port="36379", mongo_port="37117")
    redis, mongo = manifest["components"]
    assert redis["host_check"][-1] == 'test "$(redis-cli -p 36379 --raw ping)" = PONG'
    assert mongo["host_arguments"] == ["mongod", "--bind_ip", "127.0.0.1", "--port", "37117"]
    assert "--port" in mongo["host_check"] and "37117" in mongo["host_check"]


def test_redis_config_binds_loopback_only_on_the_host_network(tmp_path: Path) -> None:
    manifest = apptainer_manifest("flowcept", redis_port="36379")
    node_service_stack.private_configuration(tmp_path, manifest)
    config = (tmp_path / "redis.conf").read_text()
    assert config.startswith("bind 127.0.0.1\nport 36379\n")
    manifest["container_runtime"] = "docker"
    node_service_stack.private_configuration(tmp_path, manifest)
    assert (tmp_path / "redis.conf").read_text().startswith("bind 0.0.0.0\n")


def owned_root(tmp_path: Path) -> Path:
    root = tmp_path / "flowcept"
    root.mkdir()
    return root.resolve()


def test_start_passes_secrets_by_environment_and_binds_owned_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = owned_root(tmp_path)
    manifest = apptainer_manifest("flowcept", redis_port="36379", mongo_port="37117")
    calls: list[tuple[list[str], dict[str, str]]] = []

    def run(arguments: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append((arguments, kwargs["env"]))
        return subprocess.CompletedProcess(arguments, 0, "", "")

    images = {}
    for component in manifest["components"]:
        path = root / f"containers/images/{component['role']}.sif"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(component["role"].encode())
        images[component["name"]] = backend.digest(path)
    (root / "images.json").write_text(json.dumps(images))
    monkeypatch.setattr(backend, "instances", lambda root: {})
    monkeypatch.setattr(subprocess, "run", run)
    private = {"REDISCLI_AUTH": "s3cret", "MONGO_INITDB_ROOT_USERNAME": "clio"}
    private["MONGO_INITDB_ROOT_PASSWORD"] = "s3cret"
    backend.start(root, manifest, private)
    launches = [(args, env) for args, env in calls if args[1:3] == ["instance", "run"]]
    assert len(launches) == 2
    for args, _env in launches:
        assert "s3cret" not in " ".join(args)
        assert "--cleanenv" in args and "--containall" in args
    redis_args, redis_env = launches[0]
    assert redis_env["APPTAINERENV_REDISCLI_AUTH"] == "s3cret"
    assert f"{root}/redis.conf:/etc/redis.conf:ro" in redis_args
    assert redis_args[-2:] == ["redis-server", "/etc/redis.conf"]
    mongo_args, mongo_env = launches[1]
    assert mongo_env["APPTAINERENV_MONGO_INITDB_ROOT_PASSWORD"] == "s3cret"
    assert mongo_args[-5:] == ["mongod", "--bind_ip", "127.0.0.1", "--port", "37117"]
    checks = [args for args, _ in calls if args[1] == "exec"]
    assert checks[0][3] == f"instance://{manifest['components'][0]['name']}"


def test_start_refuses_a_sif_that_no_longer_matches_its_receipt(tmp_path: Path) -> None:
    root = owned_root(tmp_path)
    manifest = apptainer_manifest("flowcept")
    path = root / "containers/images/redis.sif"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"replaced")
    (root / "images.json").write_text(
        json.dumps({row["name"]: "sha256:" + "0" * 64 for row in manifest["components"]})
    )
    with pytest.raises(ValueError, match="no longer matches"):
        backend.start(root, manifest, {})


def test_an_instance_running_another_image_is_not_adopted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = owned_root(tmp_path)
    component = apptainer_manifest("flowcept")["components"][0]
    monkeypatch.setattr(
        backend,
        "instances",
        lambda root: {component["name"]: {"instance": component["name"], "img": "/other.sif"}},
    )
    with pytest.raises(ValueError, match="another deployment"):
        backend.owned_instance(root, component)


def test_postgres_has_a_loopback_host_network_profile() -> None:
    from tests.test_gact.test_monitoring_services import manifest

    postgres = manifest("cmf", postgres_port="35432")["components"][0]
    assert postgres["host_arguments"] == [
        "postgres",
        "-c",
        "listen_addresses=127.0.0.1",
        "-c",
        "port=35432",
    ]
    assert "-p 35432" in postgres["host_check"][-1]
    script = postgres["host_initialize"][-1]
    assert "createdb -h 127.0.0.1 -p 35432" in script
    assert subprocess.run(["sh", "-n", "-c", script], check=False).returncode == 0
    with pytest.raises(ValueError, match="distinct ports"):
        manifest("cmf", port="35432", postgres_port="35432")


def test_start_runs_the_host_initialize_step_after_readiness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = owned_root(tmp_path)
    component = {
        "name": "clio-cmf-x-postgres",
        "role": "postgres",
        "image": "docker.io/library/postgres@sha256:" + "0" * 64,
        "secrets": ["POSTGRES_PASSWORD"],
        "host_check": ["true"],
        "host_initialize": ["sh", "-c", "createdb clio"],
    }
    path = root / "containers/images/postgres.sif"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"pg")
    (root / "images.json").write_text(json.dumps({component["name"]: backend.digest(path)}))
    calls: list[list[str]] = []

    def run(arguments: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(arguments)
        return subprocess.CompletedProcess(arguments, 0, "", "")

    monkeypatch.setattr(backend, "instances", lambda root: {})
    monkeypatch.setattr(subprocess, "run", run)
    backend.start(root, {"components": [component]}, {"POSTGRES_PASSWORD": "pw"})
    execs = [args for args in calls if args[1] == "exec"]
    assert [args[4:] for args in execs] == [["true"], ["sh", "-c", "createdb clio"]]
