"""Independent monitoring definitions, fresh provenance proof and ownership guards."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import httpx
import pytest

from clio_agent.gact.infrastructure import monitoring_verify, node_service_stack
from clio_agent.gact.infrastructure.drivers import build_driver_plan, service_definitions
from clio_agent.gact.infrastructure.models import InfrastructureTarget, TargetFacts
from clio_agent.gact.infrastructure.monitoring_services import CMF_REVISION, verification_document
from clio_agent.gact.provenance.factory import _flowcept_config
from clio_agent.gact.provenance.flowcept import FlowceptProviderConfig


def facts() -> TargetFacts:
    return TargetFacts(
        target_id="node",
        label="Node",
        os="linux",
        arch="x86_64",
        accelerator="none",
        uv_available=True,
        docker_available=True,
        agent_data_root="/data/clio",
        hostname="node",
    )


def manifest(service: str, **configuration: str) -> dict[str, Any]:
    plan = build_driver_plan(
        service_id=service,
        action="install",
        variant_id="managed",
        configuration=configuration,
        facts=facts(),
        target=InfrastructureTarget(id="node", label="Node", kind="ssh"),
    )
    assert plan.readiness is not None and plan.readiness.capability == "installed"
    assert plan.retain_record
    return json.loads(plan.commands[-1].stdin)["manifest"]


def test_monitoring_is_independent_of_gpu_models_and_other_backend() -> None:
    rows = {row.id: row for row in service_definitions(facts())}
    assert rows["flowcept"].category == rows["cmf"].category == "monitoring"
    assert rows["cmf"].variants[0].compatible
    flowcept, cmf = manifest("flowcept"), manifest("cmf")
    assert "vllm" not in flowcept["project"] and "flowcept" not in cmf["project"]
    assert {row["role"] for row in flowcept["components"]} == {"redis", "mongo"}
    assert {row["role"] for row in cmf["components"]} == {"postgres", "server"}
    assert cmf["build"]["revision"] == CMF_REVISION
    assert flowcept["persistence_owner"] == "managed_collector"


def test_deployment_requires_distinct_ports_and_immutable_image() -> None:
    with pytest.raises(ValueError, match="distinct ports"):
        manifest("flowcept", port="16379")
    with pytest.raises(ValueError, match="immutable"):
        manifest("cmf", server_image="server:latest")
    cmf = manifest("cmf", server_image="sha256:" + "f" * 64)
    assert "build" not in cmf
    assert cmf["components"][-1]["image"] == "sha256:" + "f" * 64


def test_private_credentials_are_generated_on_host_and_not_in_manifest(tmp_path: Path) -> None:
    config = manifest("flowcept")
    private = node_service_stack.private_configuration(tmp_path, config)
    password = private["MONGO_INITDB_ROOT_PASSWORD"]
    assert password not in json.dumps(manifest("flowcept"))
    assert password in (tmp_path / "settings.yaml").read_text()
    assert node_service_stack.private_configuration(tmp_path, config) == private


@pytest.mark.parametrize("missing_edge", [False, True])
def test_cmf_verification_requires_both_exact_artifact_edges(
    monkeypatch: pytest.MonkeyPatch, missing_edge: bool
) -> None:
    held: dict[str, Any] = {}

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/mlmd_push":
            held.update(json.loads(json.loads(request.content)["json_payload"]))
            if missing_edge:
                held["Pipeline"][0]["stages"][0]["executions"][0]["events"].pop()
            return httpx.Response(200, json={"status": "success"})
        assert request.url.path == "/mlmd_pull"
        return httpx.Response(200, json=json.dumps(held))

    client = httpx.Client(transport=httpx.MockTransport(respond))
    monkeypatch.setattr(monitoring_verify.httpx, "Client", lambda **_: client)
    config = {"verification_document": verification_document()}
    if missing_edge:
        with pytest.raises(RuntimeError, match="lineage"):
            monitoring_verify.verify_cmf(config, "http://cmf", "fresh-probe")
    else:
        result = monitoring_verify.verify_cmf(config, "http://cmf", "fresh-probe")
        assert result["input_output_lineage"] and result["execution_id"] == "fresh-probe"


def test_container_name_collision_is_never_adopted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / "owner.json").write_text(json.dumps({"owner": "ours"}))

    def run(root: Path, arguments: list[str], **kwargs: Any) -> str:
        return (
            "same-name\n"
            if arguments[0] == "ps"
            else json.dumps([{"Config": {"Labels": {node_service_stack.OWNER_LABEL: "theirs"}}}])
        )

    monkeypatch.setattr(node_service_stack, "command", run)
    with pytest.raises(ValueError, match="another deployment"):
        node_service_stack.inspect(tmp_path, "container", "same-name")


def test_collector_ownership_is_explicit_in_provider_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CLIO_FLOWCEPT_PERSISTENCE_OWNER", "collector")
    assert _flowcept_config().persistence_owner == "collector"
    assert FlowceptProviderConfig().persistence_owner == "client"
    with pytest.raises(ValueError, match="persistence_owner"):
        FlowceptProviderConfig(persistence_owner="both")


def test_managed_flowcept_cannot_inherit_another_deployments_database(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    for name in ("MQ_URI", "KVDB_URI", "MONGO_URI", "LMDB_PATH"):
        monkeypatch.setenv(name, "another-deployment")
    node_service_stack.flowcept_environment(tmp_path)
    assert os.environ["FLOWCEPT_SETTINGS_PATH"] == str(tmp_path / "settings.yaml")
    assert not any(name in os.environ for name in ("MQ_URI", "KVDB_URI", "MONGO_URI", "LMDB_PATH"))


def test_image_capacity_uses_engine_storage_not_data_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from types import SimpleNamespace

    engine_root = tmp_path / "engine"
    monkeypatch.setattr(
        node_service_stack, "command", lambda *args: json.dumps({"DockerRootDir": str(engine_root)})
    )

    def usage(path: str) -> SimpleNamespace:
        assert Path(path) == engine_root
        return SimpleNamespace(free=5 * 1024**3)

    monkeypatch.setattr(node_service_stack.shutil, "disk_usage", usage)
    with pytest.raises(ValueError, match="image-store space"):
        node_service_stack.engine_capacity(tmp_path, 8 * 1024**3)


def test_live_logs_refresh_without_stopping_services_and_redact_credentials(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / "logs").mkdir()
    (tmp_path / "credentials.json").write_text(json.dumps({"password": "private-secret"}))
    config = manifest("flowcept")
    calls: list[list[str]] = []

    def command(root: Path, arguments: list[str], **kwargs: Any) -> str:
        calls.append(arguments)
        assert arguments[:3] == ["logs", "--tail", "100"]
        return "fresh record private-secret"

    monkeypatch.setattr(node_service_stack, "inspect", lambda *args: {"State": {"Running": True}})
    monkeypatch.setattr(node_service_stack, "command", command)
    node_service_stack.collect_logs(tmp_path, config)
    assert len(calls) == 2
    for role in ("redis", "mongo"):
        assert (tmp_path / "logs" / f"{role}.log").read_text() == "fresh record [redacted]"
    assert config["hooks"]["logs"] == "stack.py"
