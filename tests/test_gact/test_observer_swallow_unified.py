"""Slice 5 (#772): the hand-rolled observer swallows in ``routes/mcp.py`` and
``agents/builders.py`` are unified onto ``notify_tool_observer``.

An observer that raises must never break the tool call, and — unlike the old
bare ``except Exception: pass`` — the failure must be surfaced as a structured
``reason=tool_observer_failed`` log line. These tests fail against the
unfixed code (silent swallow, no log) and pass once both call sites route
through ``notify_tool_observer``.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient


class _ExplodingObserver:
    """A tool observer that raises on every lifecycle notification."""

    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []

    def __call__(self, *args: Any) -> None:
        self.calls.append(args)
        raise RuntimeError("observer boom")


class _FakeContent:
    def __init__(self, text: str) -> None:
        self.type = "text"
        self.text = text


class _FakeResult:
    def __init__(self, text: str) -> None:
        self.content = [_FakeContent(text)]
        self.data = None
        self.isError = False


class _FakeClient:
    """Async-context fastmcp.Client stand-in that returns a canned result."""

    def __init__(self, transport: Any) -> None:
        self._transport = transport

    async def __aenter__(self) -> "_FakeClient":
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        return False

    async def call_tool(self, tool_name: str, tool_args: Any) -> _FakeResult:
        return _FakeResult("hello-from-tool")


def _patch_transport(monkeypatch: pytest.MonkeyPatch, module: str) -> None:
    monkeypatch.setattr(f"{module}.transport_from_spec", lambda spec: object(), raising=True)


# --------------------------------------------------------------------------- #
# agents/builders.py :: _call_enabled_external_mcp_tool
# --------------------------------------------------------------------------- #


def test_builders_exploding_observer_does_not_break_tool_call(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import fastmcp

    from clio_agent.gact.agents import builders

    monkeypatch.setattr(fastmcp, "Client", _FakeClient, raising=True)
    monkeypatch.setattr(
        "clio_agent.tools.mcp_config.transport_from_spec", lambda spec: object(), raising=True
    )

    observer = _ExplodingObserver()
    app = SimpleNamespace(
        state=SimpleNamespace(
            pending_permission_gate=lambda name, args: "allow",
            pending_tool_observer=observer,
        )
    )
    info = {"name": "ext", "spec": {"transport": "stdio", "command": "x"}}

    with caplog.at_level(logging.WARNING):
        result = asyncio.run(
            builders._call_enabled_external_mcp_tool(app, "srv", info, "do_thing", {"a": 1})
        )

    # The tool call still succeeds despite the observer blowing up on every phase.
    assert result == "hello-from-tool"
    # started + completed both attempted (observer saw both phases).
    assert len(observer.calls) >= 2
    # ...and each failure is surfaced, not swallowed.
    assert "reason=tool_observer_failed" in caplog.text


# --------------------------------------------------------------------------- #
# routes/mcp.py :: call_external_mcp_tool
# --------------------------------------------------------------------------- #


def test_mcp_route_exploding_observer_does_not_break_tool_call(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    import fastmcp

    from clio_agent.gact.app import build_app

    monkeypatch.setattr(fastmcp, "Client", _FakeClient, raising=True)
    _patch_transport(monkeypatch, "clio_agent.gact.routes.mcp")

    app = build_app(sessions_path=tmp_path / "s.json")
    observer = _ExplodingObserver()
    app.state.external_mcp_servers = {
        "srv": {"name": "ext", "spec": {"transport": "stdio", "command": "x"}}
    }
    app.state.pending_permission_gate = lambda name, args: "allow"
    app.state.pending_tool_observer = observer

    client = TestClient(app)
    with caplog.at_level(logging.WARNING):
        resp = client.post("/v1/mcp/servers/srv/call", json={"tool": "do_thing", "args": {"a": 1}})

    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["tool"] == "do_thing"
    assert payload["content"][0]["text"] == "hello-from-tool"
    # The exploding observer's failures are surfaced, not silently swallowed.
    assert "reason=tool_observer_failed" in caplog.text
