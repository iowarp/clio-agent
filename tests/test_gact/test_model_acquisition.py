"""Model download truth, host ownership, immutable retries, and registry isolation."""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from clio_schemas.connected_resources import HostStorageLocations
from fastapi import FastAPI
from fastapi.testclient import TestClient

from clio_agent.gact.infrastructure import node_models
from clio_agent.gact.infrastructure.model_registry import (
    ModelAcquisition,
    ModelDownloadRequest,
    search_models,
)
from clio_agent.gact.infrastructure.models import (
    CommandResult,
    CommandSpec,
    CreateTargetRequest,
    SshRoute,
    TargetFacts,
    UpdateTargetRequest,
)
from clio_agent.gact.infrastructure.store import InfrastructureStore
from clio_agent.gact.routes.infrastructure import register_infrastructure_routes


def job(destination: Path | str, **changes: Any) -> dict[str, Any]:
    return {
        "id": "a" * 24,
        "repository": "org/model",
        "requested_revision": "main",
        "revision": "f" * 40,
        "destination": str(destination),
        "state": "running",
        "phase": "Downloading model files",
        "created_at": 1.0,
        "updated_at": 2.0,
        "bytes_done": 0,
        "bytes_total": None,
        "error": None,
        "pid": 123,
        "process_identity": "boot:1",
        **changes,
    }


def test_inventory_checks_files_and_process_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = tmp_path / "job"
    folder.mkdir()
    model = folder / "weights"
    model.write_bytes(b"model")
    receipt = folder / "receipt.json"
    ready = job(folder, state="ready", verified_files={"weights": [5, model.stat().st_mtime_ns]})
    node_models.write_json(receipt, ready)
    assert node_models.inspect_jobs(tmp_path)[0]["state"] == "ready"
    model.write_bytes(b"tampered")
    assert node_models.inspect_jobs(tmp_path)[0]["state"] == "stale"
    node_models.write_json(receipt, job(folder))
    monkeypatch.setattr(node_models, "process_identity", lambda pid: "different-boot:1")
    assert node_models.inspect_jobs(tmp_path)[0]["state"] == "interrupted"
    monkeypatch.setattr(node_models, "process_identity", lambda pid: "boot:1")
    observed = node_models.inspect_jobs(tmp_path)[0]
    assert observed["state"] == "running"
    assert "pid" not in observed and "process_identity" not in observed


def test_cancel_never_signals_reused_pid(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    folder = tmp_path / ("a" * 24)
    folder.mkdir()
    node_models.write_json(folder / "receipt.json", job(folder))
    monkeypatch.setattr(node_models, "process_identity", lambda pid: "other:2")

    def forbidden(*args: object) -> None:
        pytest.fail("Must not signal another process")

    monkeypatch.setattr(node_models.os, "killpg", forbidden, raising=False)
    assert node_models._cancel_locked(tmp_path, "a" * 24)["state"] == "cancelled"
    assert (folder / "cancel").exists()


@pytest.mark.parametrize("corrupt", [False, True])
def test_worker_verifies_hash_and_retries_resolved_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, corrupt: bool
) -> None:
    import huggingface_hub

    destination = tmp_path / "model"
    destination.mkdir()
    receipt = tmp_path / "receipt.json"
    node_models.write_json(receipt, job(destination))
    data = b"weights"
    requests = []

    class Api:
        def model_info(self, repository: str, **kwargs: Any) -> Any:
            requests.append(kwargs)
            return SimpleNamespace(
                sha="f" * 40,
                siblings=[
                    SimpleNamespace(
                        rfilename="weights.bin",
                        size=len(data),
                        lfs=SimpleNamespace(sha256=hashlib.sha256(data).hexdigest()),
                        blob_id=None,
                    )
                ],
            )

    def snapshot(repository: str, **kwargs: Any) -> None:
        assert kwargs["revision"] == "f" * 40
        (destination / "weights.bin").write_bytes(b"invalid" if corrupt else data)

    monkeypatch.setattr(huggingface_hub, "HfApi", Api)
    monkeypatch.setattr(huggingface_hub, "snapshot_download", snapshot)
    if corrupt:
        with pytest.raises(node_models.AcquisitionError, match="hash check"):
            node_models.download(receipt)
    else:
        node_models.download(receipt)
    result = json.loads(receipt.read_text())
    assert requests[0]["revision"] == "f" * 40
    assert result["state"] == ("failed" if corrupt else "ready")
    if corrupt:
        assert "hash check" in result["error"]
    else:
        assert node_models.complete(result)


def test_worker_redacts_auth_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import huggingface_hub

    receipt = tmp_path / "receipt.json"
    node_models.write_json(receipt, job(tmp_path))

    class Api:
        def model_info(self, *args: Any, **kwargs: Any) -> Any:
            raise httpx.HTTPStatusError(
                "secret signed URL",
                request=httpx.Request("GET", "https://registry/secret"),
                response=httpx.Response(403),
            )

    monkeypatch.setattr(huggingface_hub, "HfApi", Api)
    with pytest.raises(node_models.AcquisitionError, match="access denied") as error:
        node_models.download(receipt)
    assert error.value.__suppress_context__
    assert "secret" not in str(error.value)
    result = json.loads(receipt.read_text())
    assert result["state"] == "failed" and "access denied" in result["error"]
    assert "secret" not in receipt.read_text()


def test_worker_checks_capacity_before_downloading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import huggingface_hub

    receipt = tmp_path / "receipt.json"
    node_models.write_json(receipt, job(tmp_path))

    class Api:
        def model_info(self, *args: Any, **kwargs: Any) -> Any:
            return SimpleNamespace(
                sha="f" * 40, siblings=[SimpleNamespace(rfilename="weights.bin", size=2**40)]
            )

    def unexpected(*args: Any, **kwargs: Any) -> None:
        pytest.fail("Must reject capacity before downloading bytes")

    monkeypatch.setattr(huggingface_hub, "HfApi", Api)
    monkeypatch.setattr(huggingface_hub, "snapshot_download", unexpected)
    monkeypatch.setattr(node_models.shutil, "disk_usage", lambda path: SimpleNamespace(free=1))
    with pytest.raises(node_models.AcquisitionError, match="Insufficient space"):
        node_models.download(receipt)
    result = json.loads(receipt.read_text())
    assert result["state"] == "failed" and "Insufficient space" in result["error"]


def test_store_retains_model_ownership_across_restart_and_folder_change(tmp_path: Path) -> None:
    path = tmp_path / "infra.json"
    store = InfrastructureStore(path)
    target = store.create_target(
        CreateTargetRequest(label="Node", kind="ssh", ssh=SshRoute(profile="node"))
    )
    store.register_model_root(target.id, "/data/original")
    record = ModelAcquisition.model_validate(
        {**job("/data/original/models/m"), "target_id": target.id, "storage_root": "/data/original"}
    )
    store.put_model_acquisition(record)
    store.set_storage(target.id, HostStorageLocations(root="/data/next"))
    restored = InfrastructureStore(path)
    assert restored.model_roots(target.id) == ["/data/original"]
    assert restored.model_acquisitions(target.id)[0].state == "running"
    with pytest.raises(ValueError, match="owns model storage"):
        restored.delete_target(target.id)
    with pytest.raises(ValueError, match="different SSH route"):
        restored.update_target(
            target.id,
            UpdateTargetRequest(label="Elsewhere", kind="ssh", ssh=SshRoute(profile="other")),
        )


def test_http_reconciles_original_root_and_targets_retry_and_cancel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = FastAPI()
    register_infrastructure_routes(app, tmp_path)
    calls = []
    rows = []

    async def facts(target: Any, execute: Any = None) -> TargetFacts:
        return TargetFacts(
            target_id=target.id,
            label=target.label,
            os="linux",
            arch="x86_64",
            agent_data_root="/data/original",
            uv_available=True,
            transport_state="connected",
        )

    async def execute(target_id: str, spec: CommandSpec) -> CommandResult:
        body = json.loads(spec.stdin)
        calls.append((target_id, body))
        assert app.state.infrastructure_store.model_roots(target_id) == ["/data/original"]
        if body["action"] == "list":
            result = rows
        else:
            result = job(
                "/data/original/models/org--model--main",
                state="cancelled" if body["action"] == "cancel" else "running",
            )
            rows[:] = [result]
        return CommandResult(exit_code=0, stdout=json.dumps(result))

    monkeypatch.setattr("clio_agent.gact.routes.infrastructure_models.probe_target", facts)
    monkeypatch.setattr(app.state.infrastructure_runtime, "execute_on_target", execute)
    with TestClient(app) as client:
        route = "/v1/infrastructure/targets/local/models"
        result = client.post(route, json={"repository": "org/model"})
        assert result.status_code == 202, result.text
        assert calls[0][1]["destination"] == "/data/original/models/org--model--main"
        app.state.infrastructure_store.set_storage(
            "local", HostStorageLocations(root="/data/changed")
        )
        assert client.post(route + "/" + "a" * 24 + "/retry").status_code == 202
        assert calls[-1][1]["root"] == "/data/original"
        assert client.post(route + "/" + "a" * 24 + "/cancel").json()["state"] == "cancelled"
        assert calls[-1][1]["root"] == "/data/original"
        assert client.get(route).json()["models"][0]["destination"].startswith("/data/original")
        assert client.post(route + "/" + "b" * 24 + "/cancel").status_code == 404
        assert client.get("/v1/infrastructure/targets/missing/models").status_code == 404


def test_registry_search_contract_and_input_validation() -> None:
    async def run() -> list[dict[str, Any]]:
        def respond(request: httpx.Request) -> httpx.Response:
            assert "authorization" not in request.headers
            return httpx.Response(
                200,
                json=[
                    {
                        "id": "org/model",
                        "sha": "f" * 40,
                        "gated": "manual",
                        "pipeline_tag": "text-generation",
                    }
                ],
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            return await search_models("small", client=client)

    found = asyncio.run(run())
    assert found[0]["size_bytes"] is None and found[0]["gated"] == "manual"
    with pytest.raises(ValueError):
        ModelDownloadRequest(repository="https://example/unsafe")
    with pytest.raises(ValueError):
        ModelDownloadRequest(repository="org/model", revision="../escape")


def test_disconnected_host_keeps_receipts_and_explains_transport_first(tmp_path: Path) -> None:
    app = FastAPI()
    register_infrastructure_routes(app, tmp_path)
    store = app.state.infrastructure_store
    target = store.create_target(
        CreateTargetRequest(label="Linux node", kind="ssh", ssh=SshRoute(profile="node"))
    )
    store.register_model_root(target.id, "/data/original")
    store.put_model_acquisition(
        ModelAcquisition.model_validate(
            {
                **job("/data/original/model"),
                "target_id": target.id,
                "storage_root": "/data/original",
            }
        )
    )
    with TestClient(app) as client:
        route = f"/v1/infrastructure/targets/{target.id}/models"
        result = client.get(route).json()
        assert len(result["models"]) == 1
        assert result["unavailable_reason"].startswith("Connect this execution host")
        assert result["errors"]
        response = client.post(route, json={"repository": "org/model"})
        assert response.status_code == 409
        assert response.json()["detail"].startswith("Connect this execution host")
