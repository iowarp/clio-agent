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
    monkeypatch.setenv("CLIO_SESSION_DELETE_SETTLE_S", "0.3")  # work that never settles
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
    monkeypatch.setenv("CLIO_SESSION_DELETE_SETTLE_S", "0.3")  # a turn that never retires
    client, _, inside, _ = _setup(tmp_path)
    runner = client.app.state.turn_runner  # type: ignore[attr-defined]
    monkeypatch.setattr(runner, "busy", lambda sid: True)

    response = client.delete(f"/v1/sessions/{inside[0]}")

    assert response.status_code == 409
    assert response.json()["error"]["details"] == {"session_id": inside[0], "handles": []}


def test_session_delete_never_settling_turn_reopens_admission(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setenv("CLIO_SESSION_DELETE_SETTLE_S", "0.3")
    client, _, inside, _ = _setup(tmp_path)
    runner = client.app.state.turn_runner  # type: ignore[attr-defined]
    monkeypatch.setattr(runner, "busy", lambda sid: True)

    assert client.delete(f"/v1/sessions/{inside[0]}").status_code == 409

    assert not runner.retired(inside[0])  # the conversation stays usable


class _Fleet:
    """An agent whose workspace fleet records each close it is asked for."""

    def __init__(self) -> None:
        self.closed: list[str] = []

    def request_fleet_restart(self, root: str) -> str:
        self.closed.append(root)
        return "restarted_live"


def test_session_delete_stops_running_work_and_admits_nothing_after(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    client, _, inside, _ = _setup(tmp_path)
    runner = client.app.state.turn_runner  # type: ignore[attr-defined]
    stopped: set[str] = set()
    real_request_stop = runner.request_stop

    def request_stop(sid: str) -> object:
        stopped.add(sid)
        return real_request_stop(sid)

    monkeypatch.setattr(runner, "request_stop", request_stop)
    monkeypatch.setattr(runner, "busy", lambda sid: sid == inside[0] and sid not in stopped)

    assert client.delete(f"/v1/sessions/{inside[0]}").status_code == 204

    assert stopped == {inside[0]}
    assert client.get(f"/v1/sessions/{inside[0]}").status_code == 404
    assert runner.retired(inside[0])  # a wake or task outcome now admits no turn


def test_deleting_the_last_conversation_stops_the_workspace_fleet(tmp_path: Path) -> None:
    client, wid, inside, _ = _setup(tmp_path)
    fleet = _Fleet()
    client.app.state.agent = fleet  # type: ignore[attr-defined]
    root = client.get(f"/v1/workspaces/{wid}").json()["root_path"]

    assert client.delete(f"/v1/sessions/{inside[0]}").status_code == 204
    assert fleet.closed == []  # the other conversation still uses it

    assert client.delete(f"/v1/sessions/{inside[1]}").status_code == 204
    assert fleet.closed == [root]


def test_workspace_delete_stops_its_fleet(tmp_path: Path) -> None:
    client, wid, _, _ = _setup(tmp_path)
    fleet = _Fleet()
    client.app.state.agent = fleet  # type: ignore[attr-defined]
    root = client.get(f"/v1/workspaces/{wid}").json()["root_path"]

    assert client.delete(f"/v1/workspaces/{wid}").status_code == 204

    assert fleet.closed == [root]
