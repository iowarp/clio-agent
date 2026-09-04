"""Execution-path routing through the single MCP client factory (#1106).

Covers the two gact execution sites the factory now owns:

* ``agents/builders._call_enabled_external_mcp_tool`` -- the dynamic-agent
  external tool call (a real ``call_tool``, not introspection) must construct
  its client through ``make_mcp_client``.
* ``routes/mcp.call_external_mcp_tool`` -- a missing local dependency must read
  as ``503 dependency_missing`` and must NOT fire after the tool-start observer
  event (never a post-start ``502`` upstream fault).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient


class _FakeContent:
    def __init__(self, text: str) -> None:
        self.type = "text"
        self.text = text

    def model_dump(self, **_kwargs: Any) -> dict[str, str]:
        return {"type": self.type, "text": self.text}


class _FakeResult:
    def __init__(self, text: str) -> None:
        self.content = [_FakeContent(text)]
        self.data = None
        self.isError = False
        self.structured_content = None
        self.meta = None


class _FakeClient:
    """Async-context fastmcp.Client stand-in returning a canned result."""

    def __init__(self, target: Any, **_kwargs: Any) -> None:
        self.target = target

    async def __aenter__(self) -> "_FakeClient":
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        return False

    async def call_tool(
        self, tool_name: str, tool_args: Any, *, progress_handler: Any = None
    ) -> _FakeResult:
        return _FakeResult("hello-from-tool")


class _RecordingObserver:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []

    def __call__(self, *args: Any) -> None:
        self.calls.append(args)


# --------------------------------------------------------------------------- #
# Finding #2: builders dynamic-agent tool call routes through make_mcp_client
# --------------------------------------------------------------------------- #


def test_builders_external_tool_call_routes_through_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from clio_agent.gact.agents import builders

    calls: list[Any] = []

    def spy(target: Any, **kwargs: Any) -> _FakeClient:
        calls.append(target)
        return _FakeClient(target)

    monkeypatch.setattr("clio_agent.tools.mcp_config.transport_from_spec", lambda spec: "TSPORT")
    # #1113: builders now builds via make_elicitation_client, which still routes
    # through the ONE make_mcp_client factory (plus the wired elicitation handler).
    monkeypatch.setattr("clio_agent.tools.mcp_runtime.make_mcp_client", spy)

    observer = _RecordingObserver()
    app = SimpleNamespace(
        state=SimpleNamespace(
            pending_permission_gate=lambda name, args: "allow",
            pending_tool_observer=observer,
            # session resolution for the elicitation invocation context (#1113)
            sessions=SimpleNamespace(list=lambda: [], get=lambda sid: None),
        )
    )
    info = {"name": "ext", "spec": {"transport": "stdio", "command": "x"}}

    result = asyncio.run(
        builders._call_enabled_external_mcp_tool(app, "srv", info, "do_thing", {"a": 1})
    )

    assert result == "hello-from-tool"
    # The client was constructed via the factory, given the resolved transport.
    assert calls == ["TSPORT"]


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
