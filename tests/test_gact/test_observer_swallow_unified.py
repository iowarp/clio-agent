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

    def model_dump(self, **_kwargs: Any) -> dict[str, str]:
        """Match the public FastMCP content serialization used in production."""

        return {"type": self.type, "text": self.text}


class _FakeResult:
    def __init__(
        self,
        text: str,
        *,
        structured_content: Any = None,
        meta: Any = None,
    ) -> None:
        self.content = [_FakeContent(text)]
        self.data = None
        self.isError = False
        self.structured_content = structured_content
        self.meta = meta


class _FakeClient:
    """Async-context fastmcp.Client stand-in that returns a canned result."""

    def __init__(self, transport: Any, **_: Any) -> None:
        self._transport = transport

    async def __aenter__(self) -> "_FakeClient":
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        return False

    async def call_tool(
        self, tool_name: str, tool_args: Any, *, progress_handler: Any = None
    ) -> _FakeResult:
        return _FakeResult("hello-from-tool")


class _StructuredFakeClient(_FakeClient):
    """Return text for the model and structured MCP content for telemetry."""

    async def call_tool(
        self, tool_name: str, tool_args: Any, *, progress_handler: Any = None
    ) -> _FakeResult:
        return _FakeResult(
            "human projection",
            structured_content={
                "schema_version": "example.result.v1",
                "job_id": "job-structured",
                "state": "succeeded",
            },
            meta={"private": {"token": "must-not-reach-observer"}},
        )


class _RecordingObserver:
    """Capture lifecycle notifications without changing tool behavior."""

    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []

    def __call__(self, *args: Any) -> None:
        self.calls.append(args)


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


def test_builders_observer_receives_structured_mcp_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """External MCP telemetry retains structuredContent while the model gets text."""

    import fastmcp

    from clio_agent.gact.agents import builders

    monkeypatch.setattr(fastmcp, "Client", _StructuredFakeClient, raising=True)
    monkeypatch.setattr(
        "clio_agent.tools.mcp_config.transport_from_spec", lambda spec: object(), raising=True
    )

    observer = _RecordingObserver()
    app = SimpleNamespace(
        state=SimpleNamespace(
            pending_permission_gate=lambda name, args: "allow",
            pending_tool_observer=observer,
        )
    )
    info = {"name": "ext", "spec": {"transport": "stdio", "command": "x"}}

    result = asyncio.run(
        builders._call_enabled_external_mcp_tool(app, "srv", info, "do_thing", {"a": 1})
    )

    assert result == "human projection"
    completed = next(call for call in observer.calls if call[2] == "completed")
    assert completed[4] == {
        "content": [{"type": "text", "text": "human projection"}],
        "structuredContent": {
            "schema_version": "example.result.v1",
            "job_id": "job-structured",
            "state": "succeeded",
        },
    }
    assert "_meta" not in completed[4]


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


def test_mcp_route_observer_receives_structured_mcp_result(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Direct MCP calls persist public structured results without private metadata."""

    import fastmcp

    from clio_agent.gact.app import build_app

    monkeypatch.setattr(fastmcp, "Client", _StructuredFakeClient, raising=True)
    _patch_transport(monkeypatch, "clio_agent.gact.routes.mcp")

    app = build_app(sessions_path=tmp_path / "s.json")
    observer = _RecordingObserver()
    app.state.external_mcp_servers = {
        "srv": {"name": "ext", "spec": {"transport": "stdio", "command": "x"}}
    }
    app.state.pending_permission_gate = lambda name, args: "allow"
    app.state.pending_tool_observer = observer

    with TestClient(app) as client:
        response = client.post(
            "/v1/mcp/servers/srv/call",
            json={"tool": "do_thing", "args": {"a": 1}},
        )

    assert response.status_code == 200, response.text
    assert response.json()["content"] == [{"type": "text", "text": "human projection"}]
    completed = next(call for call in observer.calls if call[2] == "completed")
    assert completed[4] == {
        "content": [{"type": "text", "text": "human projection"}],
        "structuredContent": {
            "schema_version": "example.result.v1",
            "job_id": "job-structured",
            "state": "succeeded",
        },
    }
    assert "_meta" not in completed[4]
