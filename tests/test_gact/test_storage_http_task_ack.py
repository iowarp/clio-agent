"""REST acceptance returns the durable shared identity of real storage work."""

from __future__ import annotations

import hashlib
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from clio_agent.gact.app import build_app
from clio_agent.gact.storage.models import TransferOperation
from clio_agent.platform_paths import win_extended_path


@pytest.mark.parametrize("action,kind", [("link", "Indexing"), ("transfer", "Download")])
def test_storage_http_ack_contains_persisted_queryable_handle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, action: str, kind: str
) -> None:
    """Join the actual 202 response, owner ledger, shared query and terminal result."""
    monkeypatch.setenv("CLIO_AGENT_HOME", str(tmp_path / "agent"))
    upstream, workspace = tmp_path / "upstream", tmp_path / "workspace"
    upstream.mkdir()
    workspace.mkdir()
    payload = b"actual REST storage acceptance\n"
    (upstream / "payload.txt").write_bytes(payload)
    app = build_app(sessions_path=tmp_path / "sessions.json")
    with TestClient(app) as client:
        created = client.post(
            "/v1/workspaces", json={"name": "Task receipt", "root_path": str(workspace)}
        )
        assert created.status_code == 201, created.text
        wid = created.json()["id"]
        session = client.post("/v1/sessions", json={"workspace_id": wid, "title": "owner"})
        assert session.status_code == 200, session.text
        sid = session.json()["id"]
        source = client.post(
            f"/v1/workspaces/{wid}/sources",
            json={"provider": "local", "root": str(upstream), "label": "Owned data"},
        )
        assert source.status_code == 201, source.text
        prefix = f"/v1/workspaces/{wid}/sources/{source.json()['id']}"
        accepted = client.post(prefix + f"/{action}", json={"session_id": sid})
        assert accepted.status_code == 202, accepted.text
        receipt = accepted.json()
        operation = receipt["indexing_operation"] if action == "link" else receipt
        handle = operation["task_handle"]
        assert isinstance(handle, str) and handle.startswith("task_")
        persisted = app.state.connected_storage.store.get(
            "operation", operation["id"], TransferOperation
        )
        assert persisted.task_handle == handle and persisted.owner_session_id == sid
        snapshot = client.get(f"/v1/sessions/{sid}/async-tasks", params={"handle": handle})
        assert snapshot.status_code == 200, snapshot.text
        task = snapshot.json()["tasks"]
        assert len(task) == 1 and task[0]["task_kind"] == kind
        assert task[0]["owner"]["session_id"] == sid
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            result = client.get(f"/v1/sessions/{sid}/async-tasks/{handle}/result")
            if result.json().get("status") == "completed":
                break
            time.sleep(0.01)
        else:
            pytest.fail("Actual storage owner did not complete")
        assert result.status_code == 200, result.text
        assert result.json()["result"]["operation"]["task_handle"] == handle
        if kind == "Download":
            local = Path(result.json()["result"]["source"]["local_path"]) / "payload.txt"
            actual = Path(win_extended_path(local)).read_bytes()
            assert actual == payload
            assert hashlib.sha256(actual).digest() == hashlib.sha256(payload).digest()
        else:
            assert result.json()["result"]["operation"]["entries_done"] == 1
