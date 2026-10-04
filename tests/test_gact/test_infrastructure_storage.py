"""Persistent host paths, remote routing, capacity and existing deployment ownership."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from clio_schemas.connected_resources import HostStorageLocations
from fastapi import FastAPI
from fastapi.testclient import TestClient

from clio_agent.gact.infrastructure.models import (
    CommandResult,
    CommandSpec,
    CreateTargetRequest,
    InfrastructureTarget,
    SshRoute,
    TargetFacts,
    UpdateTargetRequest,
)
from clio_agent.gact.infrastructure.storage import (
    StorageInspectionRequest,
    inspect_target_path,
    resolved_locations,
)
from clio_agent.gact.infrastructure.storage_probe import inspect_path
from clio_agent.gact.infrastructure.store import InfrastructureStore
from clio_agent.gact.routes.infrastructure import register_infrastructure_routes


def test_storage_persists_for_local_and_remote_hosts(tmp_path: Path) -> None:
    file = tmp_path / "infra.json"
    store = InfrastructureStore(file)
    remote = store.create_target(
        CreateTargetRequest(
            label="Homelab",
            kind="ssh",
            ssh=SshRoute(profile="homelab"),
        )
    )
    local_locations = HostStorageLocations(root=str(tmp_path / "local"))
    remote_locations = HostStorageLocations(root="/data/clio-beta3", models="/data/models")
    store.set_storage("local", local_locations)
    store.set_storage(remote.id, remote_locations)
    restored = InfrastructureStore(file)
    assert restored.target("local").storage == local_locations
    assert restored.target(remote.id).storage == remote_locations
    restored.update_target(
        remote.id,
        UpdateTargetRequest(
            label="Homelab renamed",
            kind="ssh",
            ssh=SshRoute(profile="homelab"),
        ),
    )
    assert restored.target(remote.id).storage == remote_locations


def test_paths_resolve_on_target_instead_of_controller() -> None:
    target = InfrastructureTarget(
        id="h", label="Homelab", kind="ssh", storage=HostStorageLocations(root="/data/clio")
    )
    facts = TargetFacts(target_id="h", label="Homelab", os="linux", arch="x86_64")
    locations = resolved_locations(target, facts)
    assert locations.models == "/data/clio/models"
    assert locations.temporary == "/data/clio/tmp"


def test_inspection_checks_existing_ancestor_without_creating_folders(tmp_path: Path) -> None:
    path = tmp_path / "not-created" / "models"
    report = inspect_path(str(path))
    assert report["existing_ancestor"] == str(tmp_path)
    assert report["free_bytes"] > 0
    assert not report["exists"]
    assert not path.parent.exists()
    listing = tmp_path / "listing"
    listing.mkdir()
    (listing / "folder").mkdir()
    (listing / "file").write_text("test")
    assert inspect_path(str(listing), browse=True)["entries"] == [
        {"name": "folder", "path": str(listing / "folder")},
    ]


def test_remote_inspection_uses_transport_and_reports_insufficient_space() -> None:
    target = InfrastructureTarget(id="h", label="Homelab", kind="ssh", transport_state="connected")
    executed: list[CommandSpec] = []

    async def execute(spec: CommandSpec) -> CommandResult:
        executed.append(spec)
        return CommandResult(
            exit_code=0,
            stdout=json.dumps(
                {
                    "path": "/data/clio",
                    "free_bytes": 5,
                    "writable": True,
                }
            ),
        )

    report = asyncio.run(
        inspect_target_path(
            target,
            StorageInspectionRequest(path="/data/clio", required_bytes=10),
            execute,
        )
    )
    assert not report["capacity_ok"]
    assert report["target_id"] == "h"
    assert len(executed) == 1
    assert executed[0].program == "python3"
    assert json.loads(executed[0].stdin)["path"] == "/data/clio"
    target.transport_state = "disconnected"
    with pytest.raises(ValueError, match="Connect the selected host"):
        asyncio.run(
            inspect_target_path(target, StorageInspectionRequest(path="/data/clio"), execute)
        )
    assert len(executed) == 1


def test_storage_http_checks_host_and_saves_without_moving_data(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = FastAPI()
    register_infrastructure_routes(app, tmp_path)

    async def facts(target: InfrastructureTarget, execute: object = None) -> TargetFacts:
        return TargetFacts(
            target_id=target.id,
            label=target.label,
            os="windows",
            arch="x86_64",
            agent_data_root=str(tmp_path),
        )

    monkeypatch.setattr("clio_agent.gact.routes.infrastructure_storage.probe_target", facts)
    with TestClient(app) as client:
        assert client.get("/v1/infrastructure/targets/missing/storage").status_code == 404
        result = client.put(
            "/v1/infrastructure/targets/local/storage", json={"root": str(tmp_path / "data")}
        )
        assert result.status_code == 200, result.text
        assert result.json()["effective"]["models"] == str(tmp_path / "data" / "models")
        assert result.json()["defaults"]["models"] == str(tmp_path / "models")
        assert not (tmp_path / "data").exists()
        inspection = client.post(
            "/v1/infrastructure/targets/local/storage/inspect",
            json={
                "path": str(tmp_path),
                "required_bytes": 2**60,
            },
        )
        assert inspection.status_code == 200
        assert inspection.json()["capacity_ok"] is False
        assert (
            client.put("/v1/infrastructure/targets/local/storage", json={"root": "/"}).status_code
            == 422
        )
