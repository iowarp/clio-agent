"""Native service ownership, truthful readiness and pinned probe activation."""

from __future__ import annotations

import asyncio
import json
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from clio_agent.gact.infrastructure import node_service
from clio_agent.gact.infrastructure.drivers import build_driver_plan
from clio_agent.gact.infrastructure.models import (
    CommandResult,
    InfrastructureTarget,
    ServiceActionRequest,
    TargetFacts,
)
from clio_agent.gact.infrastructure.native_vllm import (
    ATTENTION_PROFILE,
    CONNECTOR_REVISION,
    FLOWCEPT_REVISION,
    native_variants,
)
from clio_agent.gact.infrastructure.service_observation import parse_observation
from clio_agent.gact.infrastructure.service_readiness import ServerExitedError, wait_until_ready
from clio_agent.gact.infrastructure.service_settlement import settle_service
from clio_agent.gact.infrastructure.store import InfrastructureStore


def facts(**values: Any) -> TargetFacts:
    return TargetFacts.model_validate(
        {
            "target_id": "node",
            "label": "Node",
            "os": "linux",
            "arch": "x86_64",
            "accelerator": "nvidia",
            "uv_available": True,
            "hostname": "node",
            "agent_data_root": "/data/clio",
            **values,
        }
    )


def plan(action: str = "install", **configuration: str) -> Any:
    return build_driver_plan(
        service_id="vllm",
        action=action,
        variant_id="native-cuda-attention",
        configuration={
            "model": "/data/models/qualified",
            "flowcept_settings": "/data/provenance/settings.yaml",
            **configuration,
        },
        facts=facts(),
        target=InfrastructureTarget(id="node", label="Node", kind="ssh"),
        api_key="test-secret-value",
    )


def test_native_compatibility_uses_host_facts_not_container_availability() -> None:
    assert all(item.compatible for item in native_variants(facts(docker_available=False)))
    for override in (
        {"os": "windows"},
        {"accelerator": "none"},
        {"uv_available": False},
        {"arch": "unknown"},
    ):
        assert not any(item.compatible for item in native_variants(facts(**override)))


def test_native_install_and_start_are_distinct_and_pinned() -> None:
    install = plan()
    assert install.readiness.capability == "installed"
    payload = json.loads(install.commands[-1].stdin)
    manifest = payload["manifest"]
    assert manifest["definition_version"] == ATTENTION_PROFILE
    assert "vllm==0.27.0" in manifest["project"]
    assert CONNECTOR_REVISION in manifest["project"] and FLOWCEPT_REVISION in manifest["project"]
    assert "test-secret" not in install.commands[-1].stdin
    script = manifest["launcher"]
    assert (
        script.index("install_probe()")
        < script.index("from flowcept")
        < script.index("runpy.run_module(")
    )
    assert "start_persistence=False" in script
    assert "VLLM_ENABLE_V1_MULTIPROCESSING" in script
    start = plan("start")
    assert start.readiness.capability == "serving"
    assert "test-secret-value" not in repr(start.commands[0].args)
    assert json.loads(start.commands[0].stdin)["api_key"] == "test-secret-value"
    assert start.failure_cleanup
    assert (
        json.loads(start.failure_cleanup[0].stdin)["require_operation_id"]
        == json.loads(start.commands[0].stdin)["operation_id"]
    )


def test_native_remove_retains_receipt_and_delete_is_separate() -> None:
    assert plan("uninstall").retain_record
    assert not plan("delete_data").retain_record
    with pytest.raises(ValueError, match="separate deletion"):
        build_driver_plan(
            service_id="ollama",
            action="delete_data",
            variant_id="cpu",
            configuration={},
            facts=facts(),
        )


def test_native_requires_host_model_and_flowcept_configuration() -> None:
    with pytest.raises(ValueError, match="downloaded model directory"):
        plan(model="org/model")
    with pytest.raises(ValueError, match="Flowcept settings"):
        plan(flowcept_settings="")


def test_structured_readiness_never_matches_running_false() -> None:
    definition = plan("start")
    responses = iter(
        [
            {
                "phase": "running",
                "installed": True,
                "running": True,
                "serving": False,
                "worker_alive": True,
            },
            {
                "phase": "running",
                "installed": True,
                "running": True,
                "serving": True,
                "worker_alive": True,
            },
        ]
    )
    calls = []

    async def execute(spec: Any) -> CommandResult:
        calls.append(spec)
        return CommandResult(exit_code=0, stdout=node_service.MARKER + json.dumps(next(responses)))

    asyncio.run(wait_until_ready(definition.readiness, execute, lambda progress: None, interval=0))
    assert len(calls) == 2
    observation = parse_observation(
        [
            node_service.MARKER
            + json.dumps({"phase": "stopped", "running": False, "installed": True})
        ]
    )
    assert observation.service_state == "stopped"

    async def stopped(spec: Any) -> CommandResult:
        return CommandResult(
            exit_code=0,
            stdout=node_service.MARKER + '{"phase":"failed","error":"installation failed"}',
        )

    with pytest.raises(ServerExitedError, match="installation failed"):
        asyncio.run(
            wait_until_ready(definition.readiness, stopped, lambda progress: None, interval=0)
        )


def test_settlement_preserves_data_and_installed_is_not_running(tmp_path: Path) -> None:
    store = InfrastructureStore(tmp_path / "infra.json")
    runtime = SimpleNamespace(store=store)
    request = ServiceActionRequest(
        target_id="local",
        action="install",
        variant_id="native-cuda",
        configuration={"model": "/data/model"},
    )
    receipt = node_service.MARKER + '{"phase":"stopped","installed":true,"running":false}'
    asyncio.run(settle_service(runtime, "vllm", request, None, [receipt], retain_record=True))
    assert store.service("local", "vllm").state == "stopped"
    request.action = "uninstall"
    receipt = node_service.MARKER + '{"phase":"not_installed","installed":false,"running":false}'
    asyncio.run(settle_service(runtime, "vllm", request, None, [receipt], retain_record=True))
    assert store.service("local", "vllm").state == "not_installed"
    request.action = "delete_data"
    asyncio.run(settle_service(runtime, "vllm", request, None, [receipt]))
    assert store.service("local", "vllm") is None


def test_target_refuses_foreign_directory_and_changed_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(node_service, "locked", lambda root: nullcontext())
    root = tmp_path / "native"
    root.mkdir()
    (root / "user-file").write_text("keep")
    with pytest.raises(ValueError, match="non-empty"):
        node_service.prepare(root, "deployment-one")
    assert (root / "user-file").read_text() == "keep"
    owned = tmp_path / "owned"
    node_service.prepare(owned, "deployment-one")
    with pytest.raises(ValueError, match="another deployment"):
        node_service.prepare(owned, "deployment-two")


def test_cleanup_cannot_stop_a_previous_operation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(node_service, "locked", lambda root: nullcontext())
    monkeypatch.setattr(node_service, "checked_root", lambda raw: Path(raw))
    root = tmp_path / "owned"
    node_service.prepare(root, "owner")
    node_service.write_json(
        root / "receipt.json", {"phase": "stopped", "operation_id": "old-operation"}
    )
    monkeypatch.setattr(
        node_service, "stop", lambda root: pytest.fail("Must not stop a previous operation")
    )
    result = node_service.control(
        {
            "root": str(root),
            "owner": "owner",
            "action": "stop",
            "require_operation_id": "failed-new-operation",
        }
    )
    assert result["phase"] == "stopped"


def test_reused_pid_is_not_owned(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(node_service, "identity", lambda pid: "new-boot:new-start")
    assert not node_service.alive({"pid": 12, "process_identity": "old-boot:old-start"})
