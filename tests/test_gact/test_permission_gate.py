"""iowarp/clio-agent#7: MCPToolBridge gate for destructive tools.

Drives the bridge directly (no FastMCP server needed) and asserts:
  - Allow-listed tool names skip the gate.
  - Destructive tool names register a permission row + block until
    the GACT layer responds.
  - "deny" decisions cause the bridge to raise PermissionError.
  - The same gate observes the live tool.call.* events.
"""

from __future__ import annotations

import asyncio
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from clio_agent.gact.app import (
    _make_permission_gate,
    _make_tool_observer,
    _tool_session_context,
    build_app,
)
from clio_agent.gact.permission_gate import (
    _external_mcp_permission_context,
    _normalize_mcp_tool_annotations,
)
from clio_agent.gact.types import Message, Part
from tests.test_gact.conftest import complete_turn


def test_non_destructive_tool_fast_allows(tmp_path: Path) -> None:
    app = build_app(sessions_path=tmp_path / "s.json")
    gate = _make_permission_gate(app)
    assert gate("hdf5_list_datasets", {}) == "allow"


def test_external_mcp_explicit_read_only_hint_fast_allows(tmp_path: Path) -> None:
    app = build_app(sessions_path=tmp_path / "s.json")
    gate = _make_permission_gate(app)

    decision = gate(
        "remote.lookup",
        {"resource_id": "resource-1"},
        _external_mcp_permission_context(
            {
                "readOnlyHint": True,
                "destructiveHint": False,
                "idempotentHint": True,
            }
        ),
    )

    assert decision == "allow"
    assert app.state.permissions == {}


def test_real_mcp_tool_annotations_normalize_with_protocol_aliases() -> None:
    from mcp.types import ToolAnnotations

    tool = SimpleNamespace(
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
        )
    )

    assert _normalize_mcp_tool_annotations(tool) == {
        "title": None,
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": None,
        "openWorldHint": None,
    }


@pytest.mark.parametrize(
    "annotations",
    [
        None,
        {},
        {"readOnlyHint": False},
        {"readOnlyHint": "true"},
        {"readOnlyHint": True, "destructiveHint": True},
        {"readOnlyHint": True, "idempotentHint": "true"},
    ],
    ids=[
        "missing",
        "empty",
        "false",
        "non_boolean",
        "contradictory",
        "malformed_sibling_hint",
    ],
)
def test_external_mcp_without_valid_read_only_hint_requires_permission(
    tmp_path: Path,
    annotations: Any,
) -> None:
    app = build_app(sessions_path=tmp_path / "s.json")
    gate = _make_permission_gate(app)

    decision = gate(
        "remote.submit",
        {"request_id": "request-1"},
        _external_mcp_permission_context(annotations),
    )

    # No session exists to receive an interactive decision, so reaching the
    # permission path fails closed immediately instead of invoking the tool.
    assert decision == "deny"


def test_external_mcp_non_read_only_hint_registers_pending_permission(tmp_path: Path) -> None:
    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as client:
        session_id = client.post("/v1/sessions", json={"title": "t"}).json()["id"]
        gate = _make_permission_gate(app)
        result: dict[str, str] = {}

        def call_gate() -> None:
            with _tool_session_context(session_id):
                result["decision"] = gate(
                    "remote.submit",
                    {"request_id": "request-1"},
                    _external_mcp_permission_context({"readOnlyHint": False}),
                )

        thread = threading.Thread(target=call_gate)
        thread.start()
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and not app.state.permissions:
            time.sleep(0.01)

        assert app.state.permissions
        pending = next(iter(app.state.permissions.values()))
        assert pending["session_id"] == session_id
        assert pending["status"] == "pending"
        assert pending["tool_call"]["tool_name"] == "remote.submit"
        assert pending["summary"] == "external MCP tool call: remote.submit"

        response = client.post(f"/v1/permissions/{pending['id']}", json={"action": "deny"})
        assert response.status_code == 204
        thread.join(timeout=2.0)
        assert not thread.is_alive()
        assert result["decision"] == "deny"


@pytest.mark.parametrize(
    "annotations",
    [None, {"readOnlyHint": False}, {"readOnlyHint": "true"}],
    ids=["missing", "false", "invalid"],
)
def test_dynamic_external_mcp_requires_permission_before_client_invocation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    annotations: Any,
) -> None:
    from clio_agent.gact.agents import builders

    class MustNotStartClient:
        constructed = False

        def __init__(self, transport: Any) -> None:
            MustNotStartClient.constructed = True

    import fastmcp

    monkeypatch.setattr(fastmcp, "Client", MustNotStartClient)
    app = build_app(sessions_path=tmp_path / "s.json")
    info = {
        "name": "remote",
        "spec": {"transport": "stdio", "command": "must-not-run", "args": []},
    }

    with pytest.raises(PermissionError, match="denied by permission gate"):
        asyncio.run(
            builders._call_enabled_external_mcp_tool(
                app,
                "srv",
                info,
                "submit",
                {"request_id": "request-1"},
                annotations,
            )
        )

    assert MustNotStartClient.constructed is False


def test_dynamic_external_mcp_read_only_hint_invokes_client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from clio_agent.gact.agents import builders

    class FakeClient:
        called = False

        def __init__(self, transport: Any) -> None:
            self.transport = transport

        async def __aenter__(self) -> "FakeClient":
            return self

        async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
            return None

        async def call_tool(self, name: str, args: dict[str, Any]) -> Any:
            FakeClient.called = True
            return SimpleNamespace(
                content=[SimpleNamespace(type="text", text=f"{name}:ok")],
                isError=False,
            )

    import fastmcp

    monkeypatch.setattr(fastmcp, "Client", FakeClient)
    monkeypatch.setattr(
        "clio_agent.tools.mcp_config.transport_from_spec",
        lambda spec: spec,
    )
    app = build_app(sessions_path=tmp_path / "s.json")
    info = {
        "name": "remote",
        "spec": {"transport": "stdio", "command": "fake", "args": []},
    }

    result = asyncio.run(
        builders._call_enabled_external_mcp_tool(
            app,
            "srv",
            info,
            "lookup",
            {"resource_id": "resource-1"},
            {"readOnlyHint": True},
        )
    )

    assert FakeClient.called is True
    assert result == "lookup:ok"


@pytest.mark.parametrize(
    "tool_annotations",
    [
        {},
        {"submit": {"readOnlyHint": False}},
        {"submit": {"readOnlyHint": "true"}},
    ],
    ids=["missing", "false", "invalid"],
)
def test_external_mcp_route_requires_permission_before_client_invocation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tool_annotations: dict[str, Any],
) -> None:
    class MustNotStartClient:
        constructed = False

        def __init__(self, transport: Any) -> None:
            MustNotStartClient.constructed = True

    import fastmcp

    monkeypatch.setattr(fastmcp, "Client", MustNotStartClient)
    app = build_app(sessions_path=tmp_path / "s.json")
    app.state.external_mcp_servers = {
        "mcp_ext_test": {
            "id": "mcp_ext_test",
            "name": "remote",
            "status": "ready",
            "tools": ["submit"],
            "tool_annotations": tool_annotations,
            "spec": {"transport": "stdio", "command": "must-not-run", "args": []},
        }
    }

    with TestClient(app) as client:
        response = client.post(
            "/v1/mcp/servers/mcp_ext_test/call",
            json={"tool": "submit", "args": {"request_id": "request-1"}},
        )

    assert response.status_code == 403
    assert response.json()["error"]["error"] == "permission_error"
    assert MustNotStartClient.constructed is False


def test_builtin_shell_tool_allows_safe_diagnostic_command(tmp_path: Path) -> None:
    app = build_app(sessions_path=tmp_path / "s.json")
    app.state.permission_default = "deny"
    gate = _make_permission_gate(app)

    assert gate("shell_bash", {"command": "date"}) == "allow"


def test_builtin_shell_tool_still_gates_non_diagnostic_command(tmp_path: Path) -> None:
    app = build_app(sessions_path=tmp_path / "s.json")
    app.state.permission_default = "deny"
    gate = _make_permission_gate(app)

    # ``rm`` is a destructive token, so it is NOT a safe read-only diagnostic and
    # still routes to the normal permission gate. (Read-only inspectors like
    # ``cat``/``ls`` ARE auto-allowed now — see _SAFE_READONLY_UTILS — so use a
    # genuinely state-changing command to exercise the gate.)
    assert gate("shell_bash", {"command": "rm pyproject.toml"}) == "deny"


def test_destructive_tool_blocks_until_resolved(tmp_path: Path) -> None:
    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as c:
        # Need a session for the gate to attach to.
        sid = c.post("/v1/sessions", json={"title": "t"}).json()["id"]

        gate = _make_permission_gate(app)
        result: dict[str, str] = {}

        def fire():
            result["decision"] = gate("shell.exec", {"cmd": "rm -rf /"})

        thread = threading.Thread(target=fire)
        thread.start()
        # Wait for the permission row to appear.
        for _ in range(50):
            rows = list(app.state.permissions.values())
            if rows:
                break
            time.sleep(0.05)
        else:
            pytest.fail("permission row never registered")
        pid = rows[0]["id"]
        assert rows[0]["session_id"] == sid

        # Approve via the API.
        resp = c.post(f"/v1/permissions/{pid}", json={"action": "allow"})
        assert resp.status_code == 204
        thread.join(timeout=2.0)
        assert not thread.is_alive(), "gate didn't unblock after allow"
        assert result["decision"] == "allow"


def test_permission_gate_uses_active_turn_session_over_recency(tmp_path: Path) -> None:
    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as c:
        older_sid = c.post("/v1/sessions", json={"title": "older", "mode": "plan"}).json()["id"]
        newer_sid = c.post("/v1/sessions", json={"title": "newer"}).json()["id"]
        assert newer_sid != older_sid

        gate = _make_permission_gate(app)
        with _tool_session_context(older_sid):
            decision = gate("shell.exec", {"cmd": "rm -rf /"})

        assert decision == "deny"
        rows = list(app.state.permissions.values())
        assert len(rows) == 1
        assert rows[0]["session_id"] == older_sid
        assert rows[0]["status"] == "auto_denied"


def test_deny_decision_raises_permission_error(tmp_path: Path) -> None:
    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as c:
        c.post("/v1/sessions", json={"title": "t"})
        gate = _make_permission_gate(app)
        result: dict[str, str] = {}

        def fire():
            result["decision"] = gate("file_delete", {"path": "/tmp/x"})

        thread = threading.Thread(target=fire)
        thread.start()
        for _ in range(50):
            if app.state.permissions:
                break
            time.sleep(0.05)
        pid = list(app.state.permissions)[0]
        c.post(f"/v1/permissions/{pid}", json={"action": "deny"})
        thread.join(timeout=2.0)
        assert result["decision"] == "deny"


def test_permission_policy_deny_blocks_destructive_tool(tmp_path: Path) -> None:
    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as c:
        sid = c.post("/v1/sessions", json={"title": "t"}).json()["id"]
        resp = c.put(
            "/v1/policies",
            json={
                "policies": [
                    {
                        "scope": "session",
                        "scope_id": sid,
                        "tool_name_pattern": "shell.*",
                        "action": "deny",
                    }
                ]
            },
        )
        assert resp.status_code == 200

        gate = _make_permission_gate(app)
        with _tool_session_context(sid):
            decision = gate("shell.exec", {"cmd": "rm -rf /"})

        assert decision == "deny"
        rows = list(app.state.permissions.values())
        assert len(rows) == 1
        assert rows[0]["session_id"] == sid
        assert rows[0]["status"] == "auto_denied"
        assert rows[0]["reason"] == "policy_deny"


def test_permission_policy_allow_skips_prompt(tmp_path: Path) -> None:
    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as c:
        sid = c.post("/v1/sessions", json={"title": "t"}).json()["id"]
        resp = c.put(
            "/v1/policies",
            json={
                "policies": [
                    {
                        "scope": "session",
                        "scope_id": sid,
                        "tool_name_pattern": "shell.*",
                        "action": "allow",
                    }
                ]
            },
        )
        assert resp.status_code == 200

        gate = _make_permission_gate(app)
        with _tool_session_context(sid):
            decision = gate("shell.exec", {"cmd": "echo ok"})

        assert decision == "allow"
        rows = list(app.state.permissions.values())
        assert len(rows) == 1
        assert rows[0]["session_id"] == sid
        assert rows[0]["status"] == "auto_approved"
        assert rows[0]["action"] == "allow"
        assert rows[0]["reason"] == "policy_allow"


def test_allow_session_resolution_adds_policy_and_audits_future_calls(
    tmp_path: Path,
) -> None:
    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as c:
        sid = c.post("/v1/sessions", json={"title": "t"}).json()["id"]
        gate = _make_permission_gate(app)
        decision_holder: dict[str, str] = {}

        def run_gate() -> None:
            with _tool_session_context(sid):
                decision_holder["decision"] = gate(
                    "shell.exec",
                    {"cmd": "rm -rf /tmp/old", "path": "/tmp/old"},
                )

        thread = threading.Thread(target=run_gate)
        thread.start()
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and not app.state.permissions:
            time.sleep(0.01)
        assert app.state.permissions
        pending = next(iter(app.state.permissions.values()))

        resp = c.post(f"/v1/permissions/{pending['id']}", json={"action": "allow_session"})
        assert resp.status_code == 204
        thread.join(timeout=2.0)
        assert decision_holder["decision"] == "allow"
        assert app.state.permission_policies == [
            {
                "scope": "session",
                "scope_id": sid,
                "tool_name_pattern": "shell.exec",
                "action": "allow",
                "created_from_permission_id": pending["id"],
                "path_pattern": "/tmp/old",
            }
        ]

        with _tool_session_context(sid):
            decision = gate("shell.exec", {"cmd": "rm -rf /tmp/old", "path": "/tmp/old"})

        assert decision == "allow"
        rows = list(app.state.permissions.values())
        assert [row["status"] for row in rows] == ["resolved", "auto_approved"]
        assert rows[-1]["reason"] == "policy_allow"


def _put_single_policy(
    client: TestClient,
    *,
    tool_name_pattern: str,
    action: str,
    scope: str = "session",
    scope_id: str = "",
) -> None:
    resp = client.put(
        "/v1/policies",
        json={
            "policies": [
                {
                    "scope": scope,
                    "scope_id": scope_id,
                    "tool_name_pattern": tool_name_pattern,
                    "action": action,
                }
            ]
        },
    )
    assert resp.status_code == 200, resp.text


def _last_permission(app: Any) -> dict[str, Any]:
    rows = list(app.state.permissions.values())
    assert rows
    return rows[-1]


def test_direct_delete_policy_deny_blocks_app_state_routes(tmp_path: Path) -> None:
    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as c:
        sid = c.post("/v1/sessions", json={"title": "t"}).json()["id"]

        # Session delete.
        _put_single_policy(c, tool_name_pattern="gact.session.delete", action="deny")
        resp = c.delete(f"/v1/sessions/{sid}")
        assert resp.status_code == 403
        assert c.get(f"/v1/sessions/{sid}").status_code == 200
        row = _last_permission(app)
        assert row["status"] == "auto_denied"
        assert row["tool_call"]["tool_name"] == "gact.session.delete"

        # Message delete.
        msg = Message(
            id="msg_policy_keep",
            session_id=sid,
            role="assistant",
            created_at=datetime.now(timezone.utc).isoformat(),
            updated_at=datetime.now(timezone.utc).isoformat(),
            parts=[Part(id="part_policy_keep", type="text", text="keep")],
        )
        app.state.messages.setdefault(sid, []).append(msg)
        _put_single_policy(
            c,
            tool_name_pattern="gact.message.delete",
            action="deny",
            scope_id=sid,
        )
        resp = c.delete(f"/v1/sessions/{sid}/messages/{msg.id}")
        assert resp.status_code == 403
        assert any(m.id == msg.id for m in app.state.messages[sid])
        assert _last_permission(app)["tool_call"]["tool_name"] == "gact.message.delete"

        # Context attachment delete.
        app.state.context_files.setdefault(sid, {})["notes.md"] = {
            "path": "notes.md",
            "mode": "read",
        }
        _put_single_policy(
            c,
            tool_name_pattern="gact.context_file.delete",
            action="deny",
            scope_id=sid,
        )
        resp = c.request(
            "DELETE",
            f"/v1/sessions/{sid}/context/files",
            json={"path": "notes.md"},
        )
        assert resp.status_code == 403
        assert "notes.md" in app.state.context_files[sid]

        # Task delete.
        task = c.post(f"/v1/sessions/{sid}/tasks", json={"title": "todo"}).json()
        _put_single_policy(c, tool_name_pattern="gact.task.delete", action="deny", scope_id=sid)
        resp = c.delete(f"/v1/tasks/{task['id']}")
        assert resp.status_code == 403
        assert task["id"] in app.state.session_tasks[sid]

        # Schedule delete.
        schedule = c.post(
            f"/v1/sessions/{sid}/schedules",
            json={"cron": "0 9 * * *", "question": "morning summary"},
        ).json()
        _put_single_policy(
            c,
            tool_name_pattern="gact.schedule.delete",
            action="deny",
            scope_id=sid,
        )
        resp = c.delete(f"/v1/schedules/{schedule['id']}")
        assert resp.status_code == 403
        assert app.state.schedules.get(schedule["id"]) is not None

        # Agent delete.
        c.post("/v1/agents", json={"id": "policy_agent", "title": "Policy Agent"})
        _put_single_policy(c, tool_name_pattern="gact.agent.delete", action="deny")
        resp = c.delete("/v1/agents/policy_agent")
        assert resp.status_code == 403
        assert c.get("/v1/agents/policy_agent").status_code == 200

        # Workspace delete.
        workspace = c.post("/v1/workspaces", json={"name": "policy ws"}).json()
        _put_single_policy(
            c,
            tool_name_pattern="gact.workspace.delete",
            action="deny",
            scope="workspace",
            scope_id=workspace["id"],
        )
        resp = c.delete(f"/v1/workspaces/{workspace['id']}")
        assert resp.status_code == 403
        assert c.get(f"/v1/workspaces/{workspace['id']}").status_code == 200

        # Hook delete.
        hook = c.post("/v1/hooks", json={"event": "post_message", "command": "echo ok"}).json()
        _put_single_policy(c, tool_name_pattern="gact.hook.delete", action="deny")
        resp = c.delete(f"/v1/hooks/{hook['id']}")
        assert resp.status_code == 403
        assert hook["id"] in app.state.declarative_hooks

        # External MCP server delete.
        app.state.external_mcp_servers = {
            "mcp_policy": {
                "id": "mcp_policy",
                "name": "policy",
                "spec": {"transport": "stdio", "command": "fake", "args": []},
            }
        }
        _put_single_policy(c, tool_name_pattern="gact.mcp_server.delete", action="deny")
        resp = c.delete("/v1/mcp/servers/mcp_policy")
        assert resp.status_code == 403
        assert "mcp_policy" in app.state.external_mcp_servers


def test_direct_delete_auto_approves_and_audits_user_action(tmp_path: Path) -> None:
    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as c:
        sid = c.post("/v1/sessions", json={"title": "t"}).json()["id"]
        msg = Message(
            id="msg_policy_delete",
            session_id=sid,
            role="assistant",
            created_at=datetime.now(timezone.utc).isoformat(),
            updated_at=datetime.now(timezone.utc).isoformat(),
            parts=[Part(id="part_policy_delete", type="text", text="delete me")],
        )
        app.state.messages.setdefault(sid, []).append(msg)

        resp = c.delete(f"/v1/sessions/{sid}/messages/{msg.id}")

        assert resp.status_code == 204
        assert all(m.id != msg.id for m in app.state.messages[sid])
        row = _last_permission(app)
        assert row["status"] == "auto_approved"
        assert row["action"] == "allow"
        assert row["reason"] == "user_requested_message_delete"
        assert row["tool_call"]["tool_name"] == "gact.message.delete"


def test_put_policies_rejects_malformed_policy_without_replacing_existing(
    tmp_path: Path,
) -> None:
    app = build_app(sessions_path=tmp_path / "s.json")
    existing = [
        {
            "scope": "session",
            "scope_id": "sess_existing",
            "tool_name_pattern": "shell.*",
            "action": "deny",
        }
    ]
    app.state.permission_policies = existing.copy()

    with TestClient(app) as c:
        resp = c.put(
            "/v1/policies",
            json={
                "policies": [
                    {
                        "scope": "session",
                        "tool_name_pattern": "shell.*",
                        "action": "alow",
                    },
                    {
                        "scope": "project",
                        "action": "deny",
                    },
                    "not-a-policy",
                ]
            },
        )

    assert resp.status_code == 422
    detail = resp.json()["error"]
    assert detail["error"] == "invalid_request"
    fields = {(err["index"], err["field"]) for err in detail["details"]["policy_errors"]}
    assert fields == {(0, "action"), (1, "scope"), (2, "policy")}
    assert app.state.permission_policies == existing


def test_put_policies_rejects_non_string_optional_patterns(tmp_path: Path) -> None:
    app = build_app(sessions_path=tmp_path / "s.json")

    with TestClient(app) as c:
        resp = c.put(
            "/v1/policies",
            json={
                "policies": [
                    {
                        "scope": "session",
                        "scope_id": "sess_1",
                        "tool_name_pattern": ["shell.*"],
                        "path_pattern": 123,
                        "action": "deny",
                    }
                ]
            },
        )

    assert resp.status_code == 422
    detail = resp.json()["error"]
    fields = {(err["index"], err["field"]) for err in detail["details"]["policy_errors"]}
    assert fields == {(0, "tool_name_pattern"), (0, "path_pattern")}
    assert app.state.permission_policies == []


def test_put_policies_normalizes_valid_policy_and_preserves_unknown_fields(
    tmp_path: Path,
) -> None:
    app = build_app(sessions_path=tmp_path / "s.json")

    with TestClient(app) as c:
        resp = c.put(
            "/v1/policies",
            json={
                "policies": [
                    {
                        "scope": " SESSION ",
                        "scope_id": "sess_1",
                        "tool_name_pattern": "shell.*",
                        "path_pattern": "",
                        "action": " DENY ",
                        "description": "block shell",
                    }
                ]
            },
        )

    assert resp.status_code == 200
    assert resp.json()["policies"] == [
        {
            "scope": "session",
            "scope_id": "sess_1",
            "tool_name_pattern": "shell.*",
            "path_pattern": "",
            "action": "deny",
            "description": "block shell",
        }
    ]
    assert app.state.permission_policies == resp.json()["policies"]


def test_permission_policies_persist_across_app_rebuild(tmp_path: Path) -> None:
    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as c:
        resp = c.put(
            "/v1/policies",
            json={
                "policies": [
                    {
                        "scope": "workspace",
                        "scope_id": "ws_default",
                        "tool_name_pattern": "shell.*",
                        "path_pattern": "/tmp/*",
                        "action": "ask",
                    }
                ]
            },
        )
    assert resp.status_code == 200

    rebuilt = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(rebuilt) as c:
        body = c.get("/v1/policies").json()

    assert body["policies"] == resp.json()["policies"]
    assert rebuilt.state.permission_policies == resp.json()["policies"]


def test_invalid_policy_update_does_not_overwrite_persisted_policies(tmp_path: Path) -> None:
    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as c:
        original = c.put(
            "/v1/policies",
            json={
                "policies": [
                    {
                        "scope": "session",
                        "scope_id": "sess_existing",
                        "tool_name_pattern": "shell.*",
                        "action": "deny",
                    }
                ]
            },
        ).json()["policies"]
        resp = c.put(
            "/v1/policies",
            json={"policies": [{"scope": "project", "action": "alow"}]},
        )
    assert resp.status_code == 422

    rebuilt = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(rebuilt) as c:
        body = c.get("/v1/policies").json()

    assert body["policies"] == original


def test_external_mcp_call_policy_deny_blocks_before_tool_execution(tmp_path: Path) -> None:
    app = build_app(sessions_path=tmp_path / "s.json")
    app.state.external_mcp_servers = {
        "mcp_ext_test": {
            "id": "mcp_ext_test",
            "name": "shell",
            "spec": {"transport": "stdio", "command": "should-not-run", "args": []},
        }
    }

    with TestClient(app) as c:
        sid = c.post("/v1/sessions", json={"title": "t"}).json()["id"]
        c.put(
            "/v1/policies",
            json={
                "policies": [
                    {
                        "scope": "session",
                        "scope_id": sid,
                        "tool_name_pattern": "shell.*",
                        "action": "deny",
                    }
                ]
            },
        )

        resp = c.post(
            "/v1/mcp/servers/mcp_ext_test/call",
            json={"tool": "exec", "args": {"cmd": "rm -rf /"}},
        )

        assert resp.status_code == 403
        detail = resp.json()["error"]
        assert detail["error"] == "permission_error"
        rows = list(app.state.permissions.values())
        assert len(rows) == 1
        assert rows[0]["status"] == "auto_denied"
        assert rows[0]["reason"] == "policy_deny"
        assert rows[0]["tool_call"]["tool_name"] == "shell.exec"


def test_external_mcp_call_policy_allow_executes_without_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FakeClient:
        called = False

        def __init__(self, transport: Any) -> None:
            self.transport = transport

        async def __aenter__(self) -> "FakeClient":
            return self

        async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
            return None

        async def call_tool(self, name: str, args: dict[str, Any]) -> Any:
            FakeClient.called = True
            return SimpleNamespace(
                content=[SimpleNamespace(type="text", text=f"{name}:{args['cmd']}")],
                isError=False,
            )

    import fastmcp
    import fastmcp.client.transports as transports

    monkeypatch.setattr(fastmcp, "Client", FakeClient)
    monkeypatch.setattr(
        transports, "StdioTransport", lambda command, args, env=None: (command, args)
    )

    app = build_app(sessions_path=tmp_path / "s.json")
    app.state.external_mcp_servers = {
        "mcp_ext_test": {
            "id": "mcp_ext_test",
            "name": "shell",
            "spec": {"transport": "stdio", "command": "fake", "args": []},
        }
    }

    with TestClient(app) as c:
        sid = c.post("/v1/sessions", json={"title": "t"}).json()["id"]
        c.put(
            "/v1/policies",
            json={
                "policies": [
                    {
                        "scope": "session",
                        "scope_id": sid,
                        "tool_name_pattern": "shell.*",
                        "action": "allow",
                    }
                ]
            },
        )

        resp = c.post(
            "/v1/mcp/servers/mcp_ext_test/call",
            json={"tool": "exec", "args": {"cmd": "echo ok"}},
        )

        assert resp.status_code == 200
        assert FakeClient.called is True
        body = resp.json()
        assert body["content"] == [{"type": "text", "text": "exec:echo ok"}]
        row = _last_permission(app)
        assert row["session_id"] == sid
        assert row["status"] == "auto_approved"
        assert row["reason"] == "policy_allow"
        assert row["tool_call"]["tool_name"] == "shell.exec"


def test_external_mcp_call_uses_explicit_session_for_policy_and_telemetry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FakeClient:
        def __init__(self, transport: Any) -> None:
            self.transport = transport

        async def __aenter__(self) -> "FakeClient":
            return self

        async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
            return None

        async def call_tool(self, name: str, args: dict[str, Any]) -> Any:
            return SimpleNamespace(
                content=[SimpleNamespace(type="text", text=f"{name}:{args['cmd']}")],
                isError=False,
            )

    import fastmcp
    import fastmcp.client.transports as transports

    monkeypatch.setattr(fastmcp, "Client", FakeClient)
    monkeypatch.setattr(
        transports, "StdioTransport", lambda command, args, env=None: (command, args)
    )

    app = build_app(sessions_path=tmp_path / "s.json")
    app.state.external_mcp_servers = {
        "mcp_ext_test": {
            "id": "mcp_ext_test",
            "name": "shell",
            "spec": {"transport": "stdio", "command": "fake", "args": []},
        }
    }

    with TestClient(app) as c:
        older_sid = c.post("/v1/sessions", json={"title": "older"}).json()["id"]
        newer_sid = c.post("/v1/sessions", json={"title": "newer"}).json()["id"]
        c.put(
            "/v1/policies",
            json={
                "policies": [
                    {
                        "scope": "session",
                        "scope_id": older_sid,
                        "tool_name_pattern": "shell.*",
                        "action": "allow",
                    },
                    {
                        "scope": "session",
                        "scope_id": newer_sid,
                        "tool_name_pattern": "shell.*",
                        "action": "deny",
                    },
                ]
            },
        )

        resp = c.post(
            "/v1/mcp/servers/mcp_ext_test/call",
            json={
                "session_id": older_sid,
                "tool": "exec",
                "args": {"cmd": "echo ok"},
            },
        )

        assert resp.status_code == 200
        body = resp.json()
        assert body["session_id"] == older_sid
        assert body["content"] == [{"type": "text", "text": "exec:echo ok"}]

        older_history = app.state.bus._history.get(older_sid, [])
        newer_history = app.state.bus._history.get(newer_sid, [])
        assert [
            e.type
            for e in older_history
            if e.type == "permission.resolved" or e.type.startswith("tool.call.")
        ] == [
            "permission.resolved",
            "tool.call.started",
            "tool.call.completed",
        ]
        assert older_history[0].payload["reason"] == "policy_allow"
        assert newer_history == []
        assert app.state.tool_call_ledger[older_sid][0]["name"] == "shell.exec"
        assert newer_sid not in app.state.tool_call_ledger


def test_observer_publishes_tool_call_events(tmp_path: Path) -> None:
    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as c:
        c.post("/v1/sessions", json={"title": "t"})
        observer = _make_tool_observer(app)

        observer("hdf5_list_datasets", {"path": "/tmp/x.h5"}, "started", None)
        observer("hdf5_list_datasets", {"path": "/tmp/x.h5"}, "completed", None)

        history = list(app.state.bus._history.values())[0]
        types = [e.type for e in history]
        assert "tool.call.started" in types
        assert "tool.call.completed" in types
        # Both events share the same call_id.
        started_id = next(e.payload["call_id"] for e in history if e.type == "tool.call.started")
        completed_id = next(
            e.payload["call_id"] for e in history if e.type == "tool.call.completed"
        )
        assert started_id == completed_id


def test_observer_uses_active_turn_session_over_recency(tmp_path: Path) -> None:
    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as c:
        older_sid = c.post("/v1/sessions", json={"title": "older"}).json()["id"]
        newer_sid = c.post("/v1/sessions", json={"title": "newer"}).json()["id"]
        assert newer_sid != older_sid
        observer = _make_tool_observer(app)

        with _tool_session_context(older_sid):
            observer("hdf5_list_datasets", {"path": "/tmp/x.h5"}, "started", None)
            observer("hdf5_list_datasets", {"path": "/tmp/x.h5"}, "completed", None)

        assert app.state.bus._history.get(newer_sid, []) == []
        older_history = app.state.bus._history.get(older_sid, [])
        assert [e.type for e in older_history if e.type.startswith("tool.call.")] == [
            "tool.call.started",
            "tool.call.completed",
        ]
        assert app.state.tool_call_ledger[older_sid][0]["name"] == "hdf5_list_datasets"
        assert newer_sid not in app.state.tool_call_ledger


def test_turn_context_reaches_observer_inside_executor_thread(tmp_path: Path) -> None:
    class ObserverAgent:
        def __init__(self) -> None:
            self.observer = None

        def forward(self, question: str, session_id: str):
            assert self.observer is not None
            self.observer("hdf5_list_datasets", {"path": "/tmp/x.h5"}, "started", None)
            self.observer("hdf5_list_datasets", {"path": "/tmp/x.h5"}, "completed", None)
            return SimpleNamespace(
                answer="ok",
                selected_expert="data",
                routing_rationale="",
            )

    agent = ObserverAgent()
    app = build_app(sessions_path=tmp_path / "s.json", agent=agent)
    agent.observer = _make_tool_observer(app)

    with TestClient(app) as c:
        older_sid = c.post("/v1/sessions", json={"title": "older"}).json()["id"]
        newer_sid = c.post("/v1/sessions", json={"title": "newer"}).json()["id"]
        complete_turn(c, older_sid, "inspect")

        assert app.state.bus._history.get(newer_sid, []) == []
        older_history = app.state.bus._history.get(older_sid, [])
        assert "tool.call.started" in [e.type for e in older_history]
        assert "tool.call.completed" in [e.type for e in older_history]
        assistant = c.get(f"/v1/sessions/{older_sid}/messages").json()["messages"][0]
        assert assistant["metadata"]["tools_called"][0]["name"] == "hdf5_list_datasets"
