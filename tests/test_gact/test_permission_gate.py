"""iowarp/clio-agent#7: MCPToolBridge gate for destructive tools.

Drives the bridge directly (no FastMCP server needed) and asserts:
  - Allow-listed tool names skip the gate.
  - Destructive tool names register a permission row + block until
    the GACT layer responds.
  - "deny" decisions cause the bridge to raise PermissionError.
  - The same gate observes the live tool.call.* events.
"""

from __future__ import annotations

import threading
import time
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
from tests.test_gact.conftest import complete_turn


def test_non_destructive_tool_fast_allows(tmp_path: Path) -> None:
    app = build_app(sessions_path=tmp_path / "s.json")
    gate = _make_permission_gate(app)
    assert gate("hdf5_list_datasets", {}) == "allow"


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
        assert app.state.permissions == {}


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
    monkeypatch.setattr(transports, "StdioTransport", lambda command, args: (command, args))

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
        assert app.state.permissions == {}


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
    monkeypatch.setattr(transports, "StdioTransport", lambda command, args: (command, args))

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
        assert [e.type for e in older_history] == [
            "tool.call.started",
            "tool.call.completed",
        ]
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
        assert [e.type for e in older_history] == [
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
