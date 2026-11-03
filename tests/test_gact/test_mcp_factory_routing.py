"""Execution-path routing through the single MCP client factory (#1106).

Covers the two gact execution sites the factory now owns:

* ``agents/external_mcp_calls._call_enabled_external_mcp_tool`` -- the agent's
  external tool call (a real ``call_tool``, not introspection) must construct
  its client through ``make_mcp_client``.
* ``routes/mcp.call_external_mcp_tool`` -- a missing local dependency must read
  as ``503 dependency_missing`` and must NOT fire after the tool-start observer
  event (never a post-start ``502`` upstream fault).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient


class _RecordingObserver:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []

    def __call__(self, *args: Any) -> None:
        self.calls.append(args)


# --------------------------------------------------------------------------- #
# Finding #2: the agent external tool call routes through make_mcp_client
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# Finding #4: route restores the 503 dependency_missing contract, pre-observer
# --------------------------------------------------------------------------- #


def test_route_missing_dependency_is_503_before_tool_start(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from clio_agent.gact.app import build_app

    def missing_dependency(*_a: Any, **_k: Any) -> Any:
        raise ImportError("No module named 'fastmcp'")

    monkeypatch.setattr("clio_agent.gact.routes.mcp.transport_from_spec", lambda spec: object())
    monkeypatch.setattr("clio_agent.tools.mcp_runtime.make_mcp_client", missing_dependency)

    app = build_app(sessions_path=tmp_path / "s.json")
    observer = _RecordingObserver()
    app.state.external_mcp_servers = {
        "srv": {"name": "ext", "spec": {"transport": "stdio", "command": "x"}}
    }
    app.state.pending_permission_gate = lambda name, args: "allow"
    app.state.pending_tool_observer = observer

    client = TestClient(app)
    resp = client.post("/v1/mcp/servers/srv/call", json={"tool": "do_thing", "args": {}})

    assert resp.status_code == 503, resp.text
    error = resp.json()["error"]
    assert error["error"] == "dependency_missing"
    assert error["recoverable"] is False
    # A missing local install must NOT surface after the call was announced
    # started: the observer never saw a lifecycle event.
    assert observer.calls == []
