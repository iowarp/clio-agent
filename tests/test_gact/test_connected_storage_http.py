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
        review = client.post(prefix + "/review").json()
        applied = client.post(
            prefix + "/apply", json={"review_id": review["id"], "paths": ["a.txt"]}
        )
        assert applied.status_code == 200, applied.text
        assert (upstream / "a.txt").read_text() == "reviewed"
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
