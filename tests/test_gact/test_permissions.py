"""interactive permission prompts."""

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


def test_permission_status_all_returns_audit_rows(tmp_path: Path) -> None:
    client = _client(
        tmp_path,
        perms=[
            {
                "tool_call": {"tool_name": "shell.exec", "input": {"cmd": "rm -rf x"}},
                "summary": "destructive shell command",
            }
        ],
    )
    sid = client.post("/v1/sessions", json={"title": "t"}).json()["id"]
    _turn(client, sid)
    pid = client.get("/v1/permissions?status=pending").json()["permissions"][0]["id"]
    assert client.post(f"/v1/permissions/{pid}", json={"action": "deny"}).status_code == 204

    pending = client.get("/v1/permissions?status=pending").json()
    audit = client.get("/v1/permissions?status=all").json()

    assert pending["permissions"] == []
    assert len(audit["permissions"]) == 1
    assert audit["permissions"][0]["id"] == pid
    assert audit["permissions"][0]["status"] == "resolved"
    assert audit["permissions"][0]["action"] == "deny"
    assert audit["metadata"] == {
        "session_id": "",
        "status": "all",
        "limit": 100,
        "total": 1,
        "returned": 1,
        "truncated": False,
        "total_before_filters": 1,
        "total_after_session_filter": 1,
    }


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
    assert body["metadata"]["session_id"] == s1
    assert body["metadata"]["total_before_filters"] == 2
    assert body["metadata"]["total_after_session_filter"] == 1


def test_sticky_permission_policy_survives_restart(tmp_path: Path) -> None:
    """iowarp/clio-agent#759: allow_workspace must persist across a server restart.

    Resolving a permission with ``allow_workspace`` derives a sticky policy;
    a fresh app instance over the same store directory must re-load that
    policy from disk instead of re-prompting.
    """

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
    pid = client.get("/v1/permissions?status=pending").json()["permissions"][0]["id"]

    resp = client.post(f"/v1/permissions/{pid}", json={"action": "allow_workspace"})
    assert resp.status_code == 204

    live = client.get("/v1/policies").json()["policies"]
    assert [p["tool_name_pattern"] for p in live] == ["shell.exec"]

    # Simulate a restart: a fresh store instance over the same directory.
    restarted = TestClient(build_app(sessions_path=tmp_path / "s.json", agent=_Agent([])))
    persisted = restarted.get("/v1/policies").json()["policies"]
    assert len(persisted) == 1
    assert persisted[0]["scope"] == "workspace"
    assert persisted[0]["action"] == "allow"
    assert persisted[0]["tool_name_pattern"] == "shell.exec"
    assert persisted[0]["created_from_permission_id"] == pid


def test_permission_list_metadata_reports_truncation_and_recent_first(
    tmp_path: Path,
) -> None:
    client = _client(tmp_path, perms=[{"tool_call": {"tool_name": "x"}}])
    sid = client.post("/v1/sessions", json={"title": "t"}).json()["id"]
    _turn(client, sid)
    _turn(client, sid)

    body = client.get("/v1/permissions?status=all&limit=1").json()

    assert len(body["permissions"]) == 1
    assert body["metadata"]["limit"] == 1
    assert body["metadata"]["total"] == 2
    assert body["metadata"]["returned"] == 1
    assert body["metadata"]["truncated"] is True

    recent = client.get("/v1/permissions?status=all&limit=2").json()["permissions"]
    assert len(recent) == 2
    assert recent[0]["created_at"] >= recent[1]["created_at"]
