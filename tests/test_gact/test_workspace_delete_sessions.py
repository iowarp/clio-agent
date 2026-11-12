"""Removing a workspace deletes its conversations, or nothing while one still runs work."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient
from pytest import MonkeyPatch

from clio_agent.gact.app import build_app


def _setup(tmp_path: Path) -> tuple[TestClient, str, list[str], str]:
    client = TestClient(build_app(sessions_path=tmp_path / "s.json"))
    root = tmp_path / "project"
    wid = client.post("/v1/workspaces", json={"name": "project", "root_path": str(root)}).json()[
        "id"
    ]
    inside = [client.post("/v1/sessions", json={"workspace_id": wid}).json()["id"] for _ in "ab"]
    elsewhere = client.post("/v1/sessions", json={}).json()["id"]
    return client, wid, inside, elsewhere


def test_workspace_delete_removes_its_conversations(tmp_path: Path) -> None:
    client, wid, inside, elsewhere = _setup(tmp_path)

    assert client.delete(f"/v1/workspaces/{wid}").status_code == 204

    for sid in inside:
        assert client.get(f"/v1/sessions/{sid}").status_code == 404
    assert client.get(f"/v1/sessions/{elsewhere}").status_code == 200
    assert client.get(f"/v1/workspaces/{wid}").status_code == 404


def test_workspace_delete_keeps_everything_while_a_conversation_runs_work(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    client, wid, inside, _ = _setup(tmp_path)
    runner = client.app.state.turn_runner  # type: ignore[attr-defined]
    real_busy = runner.busy
    monkeypatch.setattr(runner, "busy", lambda sid: sid == inside[1] or real_busy(sid))

    response = client.delete(f"/v1/workspaces/{wid}")

    assert response.status_code == 409
    error = response.json()["error"]
    assert error["error"] == "workspace_has_active_work"
    assert error["recoverable"] is True
    assert error["details"] == {"workspace_id": wid, "sessions": {inside[1]: []}}
    for sid in inside:
        assert client.get(f"/v1/sessions/{sid}").status_code == 200
    assert client.get(f"/v1/workspaces/{wid}").status_code == 200


def test_session_delete_running_turn_keeps_the_envelope(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    client, _, inside, _ = _setup(tmp_path)
    runner = client.app.state.turn_runner  # type: ignore[attr-defined]
    monkeypatch.setattr(runner, "busy", lambda sid: True)

    response = client.delete(f"/v1/sessions/{inside[0]}")

    assert response.status_code == 409
    assert response.json()["error"]["details"] == {"session_id": inside[0], "handles": []}
