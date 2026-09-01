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
    _gact_app_context,
    _make_permission_gate,
    _make_tool_observer,
    _tool_session_context,
    build_app,
)
from clio_agent.gact.permission_gate import (
    DenyDecision,
    _external_mcp_permission_context,
    _normalize_mcp_tool_annotations,
)
from clio_agent.gact.types import Message, Part
from clio_agent.tools.execution import SyncMCPToolExecutor
from tests.test_gact.conftest import complete_turn

# #948 S4b: default sessions run the blueprint react ``main``; route it to each
# test's ``build_app(agent=...)`` host fake.
pytestmark = pytest.mark.usefixtures("host_agent_executor")


def test_read_only_catalog_tool_fast_allows(tmp_path: Path) -> None:
    """#1032: a provably read-only call (static catalog ``read`` tag, no ``write``)
    fast-allows as the gate's FIRST branch — reads are never gated, even with no
    session. This replaces the old name-heuristic ``_is_destructive`` fast-allow."""
    app = build_app(sessions_path=tmp_path / "s.json")
    gate = _make_permission_gate(app)
    assert gate("fs_read_file", {"filepath": "x"}) == "allow"


def test_unclassified_tool_without_session_fails_closed(tmp_path: Path) -> None:
    """#1032: a non-read tool is no longer hand-allowed by a name substring. It is
    NOT read-only (not in the static catalog, no annotation), so it proceeds past the
    read fast-allow; with no session/approver it fails closed (deny) immediately rather
    than blocking on an interactive prompt."""
    app = build_app(sessions_path=tmp_path / "s.json")
    gate = _make_permission_gate(app)
    assert gate("hdf5_list_datasets", {}) == "deny"


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


def test_child_permission_lifecycle_is_visible_on_attended_root(tmp_path: Path) -> None:
    """A child owns the approval while the root stream receives an actionable mirror."""

    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as client:
        root_id = client.post("/v1/sessions", json={"title": "root"}).json()["id"]
        root = app.state.sessions.get(root_id)
        assert root is not None
        child = app.state.sessions.create(
            workspace_id=root.workspace_id,
            title="child",
            parent_session_id=root_id,
        )
        gate = _make_permission_gate(app)
        result: dict[str, str] = {}

        def call_gate() -> None:
            with _tool_session_context(child.id):
                result["decision"] = gate(
                    "remote.submit",
                    {"request_id": "request-child"},
                    _external_mcp_permission_context({"readOnlyHint": False}),
                )

        thread = threading.Thread(target=call_gate)
        thread.start()
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and not app.state.permissions:
            time.sleep(0.01)

        pending = next(iter(app.state.permissions.values()))
        permission_id = pending["id"]
        child_requested = [
            event
            for event in app.state.bus._history.get(child.id, [])
            if event.type == "permission.requested" and event.payload.get("id") == permission_id
        ]
        root_requested = [
            event
            for event in app.state.bus._history.get(root_id, [])
            if event.type == "permission.requested" and event.payload.get("id") == permission_id
        ]
        assert len(child_requested) == 1
        assert len(root_requested) == 1
        assert root_requested[0].payload["session_id"] == child.id
        assert root_requested[0].payload["forwarded_from_session_id"] == child.id
        assert root_requested[0].payload["attended_session_id"] == root_id

        response = client.post(
            f"/v1/permissions/{permission_id}",
            json={"action": "allow"},
        )
        assert response.status_code == 204
        thread.join(timeout=2.0)
        assert not thread.is_alive()
        assert result["decision"] == "allow"

        for stream_id in (child.id, root_id):
            resolved = [
                event
                for event in app.state.bus._history.get(stream_id, [])
                if event.type == "permission.resolved"
                and event.payload.get("permission_id") == permission_id
            ]
            assert len(resolved) == 1
            assert resolved[0].payload["session_id"] == child.id


def test_declared_mcp_mutation_blocks_before_transport_until_ui_resolution(
    tmp_path: Path,
) -> None:
    """Blueprint/workspace MCP tools must not bypass GACT annotation semantics."""

    class DeclaredClient:
        called = False

        async def __aenter__(self) -> "DeclaredClient":
            return self

        async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
            return None

        async def list_tools(self) -> list[Any]:
            return []

        async def call_tool(self, name: str, args: dict[str, Any]) -> Any:
            DeclaredClient.called = True
            return SimpleNamespace(data={"name": name, "args": args})

        async def read_resource(self, uri: str) -> Any:
            return SimpleNamespace(uri=uri)

    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as client:
        session_id = client.post("/v1/sessions", json={"title": "t"}).json()["id"]
        executor = SyncMCPToolExecutor(
            object(),
            timeout=1.0,
            client_factory=lambda _server: DeclaredClient(),
            preloaded_tools={
                "relay_jarvis_run": SimpleNamespace(
                    name="relay_jarvis_run",
                    inputSchema={"properties": {"pipeline_id": {"type": "string"}}},
                    annotations={"readOnlyHint": False, "destructiveHint": False},
                )
            },
            namespace_servers={"relay": object()},
        )
        result: dict[str, str] = {}

        def call_tool() -> None:
            with _gact_app_context(app), _tool_session_context(session_id):
                result["value"] = executor.call_tool(
                    "relay_jarvis_run",
                    {"pipeline_id": "pipeline-1"},
                )

        thread = threading.Thread(target=call_tool)
        thread.start()
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and not app.state.permissions:
            time.sleep(0.01)

        assert DeclaredClient.called is False
        pending = next(iter(app.state.permissions.values()))
        assert pending["status"] == "pending"
        assert pending["tool_call"] == {
            "tool_name": "relay_jarvis_run",
            "input": {"pipeline_id": "pipeline-1"},
        }

        response = client.post(f"/v1/permissions/{pending['id']}", json={"action": "allow"})
        assert response.status_code == 204
        thread.join(timeout=2.0)
        executor.close()

        assert not thread.is_alive()
        assert DeclaredClient.called is True
        assert '"name": "jarvis_run"' in result["value"]


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

        def __init__(self, transport: Any, **_: Any) -> None:
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


def test_builtin_shell_bash_is_not_auto_allowed_without_approver(tmp_path: Path) -> None:
    """#1032: the bounded ``shell_bash`` command parser (``_is_safe_shell_diagnostic``
    and friends) is DELETED. ``shell_bash`` is not catalog-``read`` tagged and the OS
    fence — not a gate-side command parser — contains its writes/egress, so it is not
    read-only. With no session/approver it fails closed (deny). Read-only diagnostics
    that used to be hand-parsed are now covered by is_read_only via catalog/annotation."""
    app = build_app(sessions_path=tmp_path / "s.json")
    gate = _make_permission_gate(app)

    assert gate("shell_bash", {"command": "date"}) == "deny"


def test_builtin_shell_tool_still_gates_non_diagnostic_command(tmp_path: Path) -> None:
    app = build_app(sessions_path=tmp_path / "s.json")
    gate = _make_permission_gate(app)

    # ``shell_bash`` is never read-only (its writes live behind the OS fence, not a
    # gate-side parser), so any command routes to the normal permission path; with no
    # session it fails closed (deny).
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
        # A sticky runtime append is stamped an explicit priority (P0.1 #1059 follow-up): it must
        # land in its own strictly-lowest band so it never collides with a migrated legacy row's
        # priority. This is the sole appended row, so it gets priority=0 (one below the default
        # minimum of 1 -- see grant_resolver.next_append_priority).
        assert app.state.permission_policies == [
            {
                "scope": "session",
                "scope_id": sid,
                "tool_name_pattern": "shell.exec",
                "action": "allow",
                "created_from_permission_id": pending["id"],
                "path_pattern": "/tmp/old",
                "priority": 0,
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
    # PUT materializes the priority band (P0.1 #1059): a single row that omits ``priority`` is
    # stamped priority=1 (unique descending by insertion index; sole row => 1). Unknown fields
    # (``description``) and normalization (trim + lowercase) are preserved.
    assert resp.json()["policies"] == [
        {
            "scope": "session",
            "scope_id": "sess_1",
            "tool_name_pattern": "shell.*",
            "path_pattern": "",
            "action": "deny",
            "description": "block shell",
            "priority": 1,
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

        def __init__(self, transport: Any, **_: Any) -> None:
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
        def __init__(self, transport: Any, **_: Any) -> None:
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
        # B5 #979.2: session creation now emits a session-attach ``boundary.granted``
        # (a ``semantic.event`` on the bus), so assert on the permission.resolved row directly
        # rather than history[0], and that the NEWER session leaked no tool/permission events.
        resolved = next(e for e in older_history if e.type == "permission.resolved")
        assert resolved.payload["reason"] == "policy_allow"
        assert [
            e.type
            for e in newer_history
            if e.type.startswith("tool.call.") or e.type == "permission.resolved"
        ] == []
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

        # B5 #979.2: the newer session's only bus entry is its creation-time boundary event;
        # what must NOT leak to it is any TOOL call from the older session's turn.
        assert [
            e.type
            for e in app.state.bus._history.get(newer_sid, [])
            if e.type.startswith("tool.call.")
        ] == []
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

        # B5 #979.2: newer session's creation-time boundary event is allowed; no TOOL leak is.
        assert [
            e.type
            for e in app.state.bus._history.get(newer_sid, [])
            if e.type.startswith("tool.call.")
        ] == []
        older_history = app.state.bus._history.get(older_sid, [])
        assert "tool.call.started" in [e.type for e in older_history]
        assert "tool.call.completed" in [e.type for e in older_history]
        assistant = c.get(f"/v1/sessions/{older_sid}/messages").json()["messages"][0]
        assert assistant["metadata"]["tools_called"][0]["name"] == "hdf5_list_datasets"


def test_gate_denies_write_tool_in_plan_mode_via_plan_acl(tmp_path: Path) -> None:
    """P1.1 #1063: a non-read write tool is auto-denied in plan mode through the built-in
    plan_acl rule (the resolver), not a hardcoded ``session.mode`` lock — with an audit row."""
    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as c:
        sid = c.post("/v1/sessions", json={"title": "t", "mode": "plan"}).json()["id"]
        gate = _make_permission_gate(app)
        with _tool_session_context(sid):
            assert gate("fs_apply_edit_write", {"filepath": "src/x.py"}) == "deny"
        rows = list(app.state.permissions.values())
        assert len(rows) == 1
        assert rows[0]["status"] == "auto_denied"
        assert rows[0]["session_id"] == sid


def test_gate_allows_plans_dir_md_write_in_plan_mode(tmp_path: Path) -> None:
    """P1.1 #1063: the SOLE writable path in plan mode — a ``*.md`` write under the plans dir
    — is allowed by the @70 carve-out (beats the @40 deny band)."""
    from clio_agent.gact.runtime.grant_resolver import plans_dir

    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as c:
        sid = c.post("/v1/sessions", json={"title": "t", "mode": "plan"}).json()["id"]
        gate = _make_permission_gate(app)
        target = str(plans_dir() / "2026-07-25-plan.md")
        with _tool_session_context(sid):
            assert gate("fs_apply_edit_write", {"filepath": target}) == "allow"


def test_gate_denies_write_tool_in_architect_mode(tmp_path: Path) -> None:
    """Architect is read-only + diff proposals: a direct write tool is denied (no plan carve-out)."""
    from clio_agent.gact.runtime.grant_resolver import plans_dir

    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as c:
        sid = c.post("/v1/sessions", json={"title": "t", "mode": "architect"}).json()["id"]
        gate = _make_permission_gate(app)
        with _tool_session_context(sid):
            # Even a plans-dir .md write is denied in architect (the carve-out is plan-only).
            assert gate("fs_apply_edit_write", {"filepath": str(plans_dir() / "p.md")}) == "deny"


def test_gate_user_allow_policy_does_not_override_plan_mode(tmp_path: Path) -> None:
    """An explicit user allow(fs_apply_edit_write, src/**) does NOT beat the plan_acl deny band."""
    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as c:
        sid = c.post("/v1/sessions", json={"title": "t", "mode": "plan"}).json()["id"]
        app.state.permission_policies = [
            {
                "scope": "session",
                "scope_id": sid,
                "tool_name_pattern": "fs_apply_edit_write",
                "path_pattern": "src/**",
                "action": "allow",
            }
        ]
        gate = _make_permission_gate(app)
        with _tool_session_context(sid):
            assert gate("fs_apply_edit_write", {"filepath": "src/x.py"}) == "deny"


def test_plan_acl_deny_surfaces_mode_aware_message(tmp_path: Path) -> None:
    """P1.2 #1064: a plan_acl-denied write returns a ``DenyDecision`` carrying the mode-aware
    message ("Plan Mode" + the plan file path), while the audit reason stays ``policy_deny``."""
    from clio_agent.gact.runtime.grant_resolver import plans_dir

    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as c:
        sid = c.post("/v1/sessions", json={"title": "t", "mode": "plan"}).json()["id"]
        gate = _make_permission_gate(app)
        with _tool_session_context(sid):
            decision = gate("fs_apply_edit_write", {"filepath": "src/x.py"})
        # Backward compatible: still equals "deny" for every legacy comparison.
        assert decision == "deny"
        assert isinstance(decision, DenyDecision)
        assert "Plan Mode" in decision.deny_message
        assert str(plans_dir()) in decision.deny_message
        assert "denied by permission gate" not in decision.deny_message
        # The typed audit reason is unchanged.
        rows = list(app.state.permissions.values())
        assert len(rows) == 1
        assert rows[0]["status"] == "auto_denied"
        assert rows[0]["reason"] == "policy_deny"


def test_user_policy_deny_in_edit_mode_carries_no_plan_message(tmp_path: Path) -> None:
    """A user-policy deny (edit mode, no plan lock) returns a plain ``"deny"`` with no message,
    so only plan-mode blocks carry the mode-aware guidance."""
    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as c:
        sid = c.post("/v1/sessions", json={"title": "t", "mode": "edit"}).json()["id"]
        app.state.permission_policies = [
            {
                "scope": "session",
                "scope_id": sid,
                "tool_name_pattern": "fs_apply_edit_write",
                "action": "deny",
            }
        ]
        gate = _make_permission_gate(app)
        with _tool_session_context(sid):
            decision = gate("fs_apply_edit_write", {"filepath": "src/x.py"})
        assert decision == "deny"
        assert getattr(decision, "deny_message", "") == ""
