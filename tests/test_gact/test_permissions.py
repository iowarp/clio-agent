"""CLIO-BBBBBBBBBB23: interactive permission prompts."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from fastapi.testclient import TestClient

from clio_agent.gact.app import build_app


@dataclass
class _Pred:
    answer: str = "ok"
    selected_expert: str = "data_expert"
    routing_rationale: str = ""
    permissions_requested: list = field(default_factory=list)


class _Agent:
    def __init__(self, perms):
        self._pred = _Pred(permissions_requested=perms)

    def forward(self, question: str, session_id: str):
        return self._pred


def _client(tmp_path: Path, perms) -> TestClient:
    return TestClient(build_app(sessions_path=tmp_path / "s.json", agent=_Agent(perms)))


def _turn(client: TestClient, sid: str) -> dict:
    return client.post(
        f"/v1/sessions/{sid}/messages",
        json={"parts": [{"type": "text", "text": "delete /tmp"}]},
    ).json()


def test_permission_requested_then_allowed(tmp_path: Path) -> None:
    client = _client(
        tmp_path,
        perms=[
            {
                "tool_call": {
                    "call_id": "c1",
                    "tool_name": "shell.exec",
                    "input": {"cmd": "rm -rf /tmp/scratch"},
                },
                "summary": "destructive shell command",
            }
        ],
    )
    sid = client.post("/v1/sessions", json={"title": "t"}).json()["id"]
    _turn(client, sid)

    body = client.get("/v1/permissions?status=pending").json()
    assert len(body["permissions"]) == 1
    pid = body["permissions"][0]["id"]
    assert body["permissions"][0]["tool_call"]["tool_name"] == "shell.exec"
    assert body["permissions"][0]["summary"] == "destructive shell command"

    resp = client.post(f"/v1/permissions/{pid}", json={"action": "allow"})
    assert resp.status_code == 204

    # After resolution the pending filter is empty.
    body = client.get("/v1/permissions?status=pending").json()
    assert body["permissions"] == []

    # Full list includes the resolved row.
    body = client.get("/v1/permissions").json()
    assert body["permissions"][0]["action"] == "allow"
    assert body["permissions"][0]["status"] == "resolved"


def test_permission_unknown_id_404s(tmp_path: Path) -> None:
    client = _client(tmp_path, perms=[])
    resp = client.post("/v1/permissions/perm_nope", json={"action": "deny"})
    assert resp.status_code == 404


def test_permission_invalid_action_422s(tmp_path: Path) -> None:
    client = _client(tmp_path, perms=[{"tool_call": {"tool_name": "x"}}])
    sid = client.post("/v1/sessions", json={"title": "t"}).json()["id"]
    _turn(client, sid)
    pid = client.get("/v1/permissions").json()["permissions"][0]["id"]
    resp = client.post(f"/v1/permissions/{pid}", json={"action": "maybe"})
    assert resp.status_code == 422


def test_permission_list_filter_by_session(tmp_path: Path) -> None:
    """?session_id= narrows to one session."""

    client = _client(tmp_path, perms=[{"tool_call": {"tool_name": "x"}}])
    s1 = client.post("/v1/sessions", json={"title": "s1"}).json()["id"]
    s2 = client.post("/v1/sessions", json={"title": "s2"}).json()["id"]
    _turn(client, s1)
    _turn(client, s2)
    body = client.get(f"/v1/permissions?session_id={s1}").json()
    assert len(body["permissions"]) == 1
    assert body["permissions"][0]["session_id"] == s1
