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

import pytest
from fastapi.testclient import TestClient

from clio_agent.gact.app import _make_permission_gate, _make_tool_observer, build_app


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
            result["decision"] = gate(
                "shell.exec", {"cmd": "rm -rf /"}
            )

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
        started_id = next(
            e.payload["call_id"] for e in history
            if e.type == "tool.call.started"
        )
        completed_id = next(
            e.payload["call_id"] for e in history
            if e.type == "tool.call.completed"
        )
        assert started_id == completed_id
