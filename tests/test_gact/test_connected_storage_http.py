"""Real source HTTP lifecycle and isolation through the connected CLIO API."""

from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from clio_agent.gact.routes.connected_storage import register_connected_storage_routes
from clio_agent.runtime.sandbox_codex_profile import synthesize_codex_profile
from clio_agent.tools.file_policy import FileAccessPolicy, FilePolicyError


def test_writable_link_uses_custody_boundary_instead_of_raw_os_grant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """HTTP approval -> agent tool -> real original, with OS bypass still refused."""
    from clio_agent.gact.resource_custody import ResourceStore
    from clio_agent.gact.storage import setup_tool

    monkeypatch.setenv("CLIO_AGENT_HOME", str(tmp_path / "agent"))
    upstream, workspace = tmp_path / "original", tmp_path / "workspace"
    upstream.mkdir()
    workspace.mkdir()
    original = upstream / "data.txt"
    original.write_text("before")
    app = FastAPI()
    app.state.workspaces = {"w": SimpleNamespace(root_path=str(workspace))}
    app.state.sessions = {"s": SimpleNamespace(workspace_id="w")}
    app.state.resource_store = ResourceStore(root=tmp_path / "resources", max_resource_bytes=1024)
    register_connected_storage_routes(app)
    monkeypatch.setattr(setup_tool.context, "active_app", lambda: app)
    monkeypatch.setattr(setup_tool.context, "active_session_id", lambda: "s")
    with TestClient(app) as client:
        source = client.post(
            "/v1/workspaces/w/sources",
            json={
                "provider": "local",
                "root": str(upstream),
                "label": "Data",
                "mode": "write_enabled",
            },
        )
        assert source.status_code == 201, source.text
        row = source.json()
        assert row["link_available"] and row["download_available"]
        assert row["local_path"] is None
        prefix = f"/v1/workspaces/w/sources/{row['id']}"
        linked = client.post(prefix + "/link")
        from tests.test_gact.storage_operation_helpers import complete_indexing

        complete_indexing(client, prefix, linked)
        policy = FileAccessPolicy(allowed_roots=(tmp_path,))
        with pytest.raises(FilePolicyError, match="read-only"):
            policy.validate_write(str(original))
        profile = synthesize_codex_profile([tmp_path])
        assert profile["filesystem"][str(upstream)] == "read"
        listing = setup_tool.connected_data_open(row["id"])
        assert listing["writes_propagate"] is True
        saved = setup_tool.connected_data_write(row["id"], "data.txt", "after", listing["revision"])
        assert saved["revision"] != listing["revision"]
        assert original.read_text() == "after"
        assert client.post(prefix + "/unlink").status_code == 200
        with pytest.raises(ValueError, match="link changed"):
            setup_tool.connected_data_write(row["id"], "data.txt", "unlinked", saved["revision"])
        assert original.read_text() == "after"


def test_connected_source_lifecycle_and_protected_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLIO_AGENT_HOME", str(tmp_path / "agent"))
    workspace = tmp_path / "workspace"
    upstream = tmp_path / "upstream"
    workspace.mkdir()
    upstream.mkdir()
    (upstream / "a.txt").write_text("initial")
    app = FastAPI()
    app.state.workspaces = {"w": SimpleNamespace(root_path=str(workspace))}
    register_connected_storage_routes(app)
    with TestClient(app) as client:
        created = client.post(
            "/v1/workspaces/w/sources",
            json={
                "provider": "local",
                "root": str(upstream),
                "label": "Data",
                "mode": "working_copy",
            },
        )
        assert created.status_code == 201, created.text
        identifier = created.json()["id"]
        prefix = f"/v1/workspaces/w/sources/{identifier}"
        assert client.post(prefix + "/browse", json={}).json()["entries"][0]["path"] == "a.txt"
        assert (
            client.get(f"/v1/workspaces/other/sources/{identifier}/operations").status_code == 404
        )
        assert client.post(prefix + "/transfer").status_code == 202
        for _ in range(100):
            operations = client.get(prefix + "/operations").json()["operations"]
            if operations[-1]["state"] in {"completed", "failed"}:
                break
            time.sleep(0.01)
        assert operations[-1]["state"] == "completed", operations
        current = client.get("/v1/workspaces/w/sources").json()["sources"][0]
        copy = Path(current["local_path"])
        (copy / "a.txt").write_text("reviewed")
        review = client.post(prefix + "/review")
        assert review.status_code == 409, review.text
        assert "Only linked folders" in review.text
        assert (upstream / "a.txt").read_text() == "initial"
        policy = FileAccessPolicy(allowed_roots=(tmp_path,))
        with pytest.raises(FilePolicyError, match="read-only"):
            policy.validate_write(str(upstream / "a.txt"))
        private = tmp_path / "agent" / "config" / "storage-auth"
        (private / "credentials.json").write_text("private")
        with pytest.raises(FilePolicyError, match="private"):
            policy.validate_read(str(private / "credentials.json"))
        profile = synthesize_codex_profile([tmp_path, upstream / "nested", private / "nested"])
        assert profile["filesystem"][str(upstream)] == "read"
        assert profile["filesystem"][str(private)] == "deny"
        assert str(upstream / "nested") not in profile["filesystem"]
        assert str(private / "nested") not in profile["filesystem"]
        from clio_agent.runtime.storage_access import require_storage_fence

        with pytest.raises(PermissionError, match="cannot enforce"):
            require_storage_fence("none", False, (workspace,))
        with pytest.raises(PermissionError, match="cannot enforce"):
            require_storage_fence("landlock", True, (tmp_path,))
        require_storage_fence("codex", True, (tmp_path,))
        from clio_agent.gact.storage.models import SourceRecord

        record = app.state.connected_storage.store.get("source", identifier, SourceRecord)
        baseline = (
            app.state.connected_storage.store.root / identifier / record.manifest_id / "a.txt"
        )
        assert FileAccessPolicy(allowed_roots=(workspace,)).validate_read(str(baseline)) == baseline
        assert client.post(prefix + "/disconnect").status_code == 200
        assert copy.exists()
        assert client.post(prefix + "/browse", json={}).status_code == 409
        assert client.post(prefix + "/remove-copy").status_code == 200
        assert not copy.exists()
        assert (upstream / "a.txt").exists()
        assert list(
            (tmp_path / "agent" / "data" / "connected-sources" / identifier).glob("snapshot_*")
        )
        # Removing the list entry is distinct from deleting retained evidence.
        assert client.delete(prefix).status_code == 200
        assert client.get("/v1/workspaces/w/sources").json()["sources"] == []
        assert baseline.exists()
        assert (upstream / "a.txt").exists()
        assert client.post(prefix + "/reconnect").status_code == 404


def test_source_edit_keeps_identity_and_remove_cannot_resurrect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Failed sign-in setup remains editable, removable and scoped to its workspace."""
    monkeypatch.setenv("CLIO_AGENT_HOME", str(tmp_path / "agent"))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    app = FastAPI()
    app.state.workspaces = {
        "w": SimpleNamespace(root_path=str(workspace)),
        "other": SimpleNamespace(root_path=str(workspace)),
    }
    register_connected_storage_routes(app)
    body = {
        "provider": "google_drive",
        "root": "https://drive.google.com/drive/folders/first?usp=sharing",
        "label": "First",
        "mode": "read_only",
    }
    with TestClient(app) as client:
        created = client.post("/v1/workspaces/w/sources", json=body)
        assert created.status_code == 201, created.text
        source = created.json()
        assert source["can_edit_location"]
        prefix = f"/v1/workspaces/w/sources/{source['id']}"
        body.update(root="https://drive.google.com/drive/folders/second", label="Renamed")
        updated = client.patch(prefix, json=body)
        assert updated.status_code == 200, updated.text
        assert updated.json()["id"] == source["id"]
        assert updated.json()["root"] == "second"
        assert updated.json()["label"] == "Renamed"
        assert not updated.json()["authenticated"]
        assert len(client.get("/v1/workspaces/w/sources").json()["sources"]) == 1
        assert client.patch(prefix.replace("/w/", "/other/"), json=body).status_code == 404
        assert client.delete(prefix.replace("/w/", "/other/")).status_code == 404
        assert client.delete(prefix).status_code == 200
        assert client.get("/v1/workspaces/w/sources").json()["sources"] == []
        assert client.patch(prefix, json=body).status_code == 404
        assert client.post(prefix + "/auth/start").status_code == 404
        assert client.post(prefix + "/reconnect").status_code == 404
    # The tombstone survives a server restart.
    register_connected_storage_routes(FastAPI())
    from clio_agent.gact.storage.service import StorageService

    reopened = StorageService(app.state.connected_storage.store.root, tmp_path / "credentials.json")
    assert reopened.sources("w") == []


def test_source_edit_and_remove_refuse_in_flight_transfers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An operation cannot publish a stale record over an edit or removal."""
    monkeypatch.setenv("CLIO_AGENT_HOME", str(tmp_path / "agent"))
    workspace, upstream = tmp_path / "workspace", tmp_path / "upstream"
    workspace.mkdir()
    upstream.mkdir()
    app = FastAPI()
    app.state.workspaces = {"w": SimpleNamespace(root_path=str(workspace))}
    register_connected_storage_routes(app)
    body = {"provider": "local", "root": str(upstream), "label": "Data", "mode": "read_only"}
    with TestClient(app) as client:
        source = client.post("/v1/workspaces/w/sources", json=body).json()
        prefix = f"/v1/workspaces/w/sources/{source['id']}"
        operation = app.state.connected_storage.store.begin_operation(source["id"], "materialize")
        assert client.patch(prefix, json={**body, "label": "New"}).status_code == 409
        assert client.delete(prefix).status_code == 409
        app.state.connected_storage.store.update_operation(operation.id, state="failed")
        assert client.patch(prefix, json={**body, "label": "New"}).status_code == 200
        assert client.patch(prefix, json={**body, "mode": "working_copy"}).status_code == 409
        assert client.delete(prefix).status_code == 200
        assert upstream.exists()
