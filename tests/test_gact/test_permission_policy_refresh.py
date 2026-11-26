"""Sticky consent covers matching tool requests that were already pending."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import pytest

from tests.test_gact.test_permissions import _client, _turn

pytestmark = pytest.mark.usefixtures("host_agent_executor")


@pytest.mark.parametrize("action", ["allow_session", "allow_workspace"])
def test_sticky_consent_wakes_matching_waiters(tmp_path: Path, action: str) -> None:
    permissions = [
        {"tool_call": {"tool_name": tool, "input": {"target": target}}}
        for tool, target in [
            ("web_fetch", "https://arrow.apache.org/docs/python/parquet.html"),
            ("web_fetch", "https://docs.dask.org/en/stable/diagnostics-distributed.html"),
            ("shell.exec", "other operation"),
        ]
    ]
    with _client(tmp_path, permissions) as client:
        app: Any = client.app
        sid = client.post("/v1/sessions", json={"title": "research"}).json()["id"]
        _turn(client, sid)
        rows = list(app.state.permissions.values())
        source, waiting = [row for row in rows if row["tool_call"]["tool_name"] == "web_fetch"]
        other = next(row for row in rows if row["tool_call"]["tool_name"] == "shell.exec")
        event = threading.Event()
        app.state.permission_events[waiting["id"]] = event
        response = client.post(f"/v1/permissions/{source['id']}", json={"action": action})
        assert response.status_code == 204, response.text
        assert event.is_set()
        assert waiting["status"] == "resolved" and waiting["action"] == "allow"
        assert waiting["reason"] == "permission_policy_updated"
        assert waiting["policy_refresh_source"] == source["id"]
        assert other["status"] == "pending"
        emitted = [
            e
            for e in app.state.bus.session_events_since(sid)
            if e.type == "permission.resolved" and e.payload.get("permission_id") == waiting["id"]
        ]
        assert emitted and emitted[-1].payload["policy_refresh_source"] == source["id"]


@pytest.mark.parametrize("action", ["allow_session", "allow_workspace"])
def test_lasting_write_allow_covers_its_folder_not_others(tmp_path: Path, action: str) -> None:
    from clio_agent.gact.runtime.permission_policies import standing_scope

    out = tmp_path / "skill" / "out"
    paths = [out / "a.txt", out / "deep" / "b.txt", tmp_path / "skill" / "c.txt"]
    permissions = [
        {"tool_call": {"tool_name": "fs_write_file", "input": {"path": str(p)}}} for p in paths
    ]
    with _client(tmp_path, permissions) as client:
        app: Any = client.app
        sid = client.post("/v1/sessions", json={"title": "write"}).json()["id"]
        _turn(client, sid)
        source, inside, sibling = list(app.state.permissions.values())
        source.update(standing_scope(source["tool_call"]["input"]))
        assert source["standing_scope"] == {"folder": str(out.resolve())}
        response = client.post(f"/v1/permissions/{source['id']}", json={"action": action})
        assert response.status_code == 204, response.text
        assert source["policy"]["path_pattern"].endswith("*")
        assert inside["status"] == "resolved" and inside["action"] == "allow"
        assert sibling["status"] == "pending"


def test_standing_scope_is_absent_for_questions_without_a_lasting_answer() -> None:
    from clio_agent.gact.runtime.permission_policies import standing_scope

    args = {"path": str(Path.cwd() / "x.txt")}
    assert standing_scope(args, ["allow", "deny"]) == {}
    assert standing_scope({"path": "rel.txt"}) == {}


@pytest.mark.parametrize("strict", [None, "deny", "ask"])
def test_sticky_consent_retains_stricter_policy_and_special_gates(
    tmp_path: Path, strict: str | None
) -> None:
    permissions = [
        {"tool_call": {"tool_name": "web_fetch", "input": {"target": f"https://example.org/{i}"}}}
        for i in range(3)
    ]
    with _client(tmp_path, permissions) as client:
        app: Any = client.app
        sid = client.post("/v1/sessions", json={"title": "research"}).json()["id"]
        _turn(client, sid)
        source, denied, special = list(app.state.permissions.values())
        special["kind"] = "turn_defer"
        if strict:
            app.state.permission_policies.append(
                {
                    "scope": "workspace",
                    "scope_id": "ws_default",
                    "tool_name_pattern": "web_fetch",
                    "action": strict,
                    "priority": 100,
                }
            )
        response = client.post(
            f"/v1/permissions/{source['id']}", json={"action": "allow_workspace"}
        )
        assert response.status_code == 204, response.text
        assert denied["status"] == ("pending" if strict else "resolved")
        assert special["status"] == "pending"


@pytest.mark.parametrize("action", ["allow_session", "allow_workspace"])
def test_pending_grants_preserve_session_and_workspace_boundaries(
    tmp_path: Path, action: str
) -> None:
    permissions = [
        {"tool_call": {"tool_name": "web_fetch", "input": {"target": "https://example.org"}}}
    ]
    with _client(tmp_path, permissions) as client:
        app: Any = client.app
        sid = client.post("/v1/sessions", json={"title": "research"}).json()["id"]
        _turn(client, sid)
        source = next(iter(app.state.permissions.values()))
        matching = app.state.sessions.create(workspace_id="ws_default", title="research child")
        different = app.state.sessions.create(
            workspace_id="different-workspace", title="other work"
        )
        for session in [matching, different]:
            app.state.permissions[session.id] = {
                **source,
                "id": session.id,
                "session_id": session.id,
            }
        response = client.post(f"/v1/permissions/{source['id']}", json={"action": action})
        assert response.status_code == 204, response.text
        assert app.state.permissions[matching.id]["status"] == (
            "resolved" if action == "allow_workspace" else "pending"
        )
        assert app.state.permissions[different.id]["status"] == "pending"
