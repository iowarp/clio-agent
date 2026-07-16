from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from clio_agent.gact.app import build_app


def _make_fake_client(tool_names: list[str], *, fail: bool = False):
    """Build a fake ``fastmcp.Client`` replacement.

    The reconnect route does ``from fastmcp import Client`` at call time
    and uses it as ``async with Client(transport) as c: await c.list_tools()``.
    Patching ``fastmcp.Client`` with this factory lets us drive the probe
    without spawning a real MCP server.
    """

    class _Tool:
        def __init__(self, name: str) -> None:
            self.name = name
            self.annotations = {
                "readOnlyHint": True,
                "destructiveHint": False,
            }

    class _FakeClient:
        def __init__(self, transport: object) -> None:
            self._transport = transport

        async def __aenter__(self) -> "_FakeClient":
            if fail:
                raise RuntimeError("connection refused")
            return self

        async def __aexit__(self, *exc: object) -> bool:
            return False

        async def list_tools(self) -> list[_Tool]:
            return [_Tool(n) for n in tool_names]

    return _FakeClient


def _make_hanging_client():
    """Build a fake ``fastmcp.Client`` whose ``list_tools`` never returns.

    Simulates a hung MCP server: the connection opens fine but the first
    ``list_tools`` round-trip blocks forever, exercising the reconnect
    route's ``asyncio.wait_for`` timeout guard (gap-523).
    """

    class _HangingClient:
        def __init__(self, transport: object) -> None:
            self._transport = transport

        async def __aenter__(self) -> "_HangingClient":
            return self

        async def __aexit__(self, *exc: object) -> bool:
            return False

        async def list_tools(self) -> list[object]:
            await asyncio.Event().wait()  # blocks until cancelled
            raise AssertionError("unreachable")  # pragma: no cover

    return _HangingClient


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    return TestClient(build_app(sessions_path=tmp_path / "sessions.json"))


def _seed_server(
    client: TestClient,
    sid: str = "mcp_ext_test01",
    *,
    transport: str = "stdio",
    tools: list[str] | None = None,
) -> str:
    if transport == "stdio":
        spec = {"transport": "stdio", "command": "echo", "args": []}
    else:
        spec = {"transport": "http", "url": "https://mcp.example.com"}
    client.app.state.external_mcp_servers = {
        sid: {
            "id": sid,
            "name": "everything",
            "status": "error",
            "transport": transport,
            "tools": tools if tools is not None else ["stale"],
            "spec": spec,
            "error": "previous failure",
        }
    }
    return sid


def test_reconnect_404_unknown_server(client: TestClient) -> None:
    resp = client.post("/v1/mcp/servers/does-not-exist/reconnect")
    assert resp.status_code == 404
    assert resp.json()["error"]["error"] == "not_found"


def test_reconnect_success_updates_tools_and_status(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    sid = _seed_server(client, tools=["stale"])
    monkeypatch.setattr("fastmcp.Client", _make_fake_client(["alpha", "beta"]))

    resp = client.post(f"/v1/mcp/servers/{sid}/reconnect")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "ready"
    assert body["tools"] == ["alpha", "beta"]
    assert body["tools_count"] == 2
    # Registry row updated in place (and the stale error cleared).
    row = client.app.state.external_mcp_servers[sid]
    assert row["status"] == "ready"
    assert row["tools"] == ["alpha", "beta"]
    assert row["tool_annotations"] == {
        "alpha": {"readOnlyHint": True, "destructiveHint": False},
        "beta": {"readOnlyHint": True, "destructiveHint": False},
    }
    assert "error" not in row


def test_reconnect_emits_reconnected_event(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    sid = _seed_server(client)
    monkeypatch.setattr("fastmcp.Client", _make_fake_client(["alpha"]))

    client.post(f"/v1/mcp/servers/{sid}/reconnect")

    # Global status events ride session_id="" (like lm.provider.*).
    history = client.app.state.bus._history.get("", [])
    reconnected = [e for e in history if e.type == "mcp.server.reconnected"]
    assert len(reconnected) == 1
    assert reconnected[0].payload["server_id"] == sid
    assert reconnected[0].payload["status"] == "ready"
    assert reconnected[0].payload["tools"] == ["alpha"]


def test_reconnect_probe_failure_returns_502_and_emits_error(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    sid = _seed_server(client)
    monkeypatch.setattr("fastmcp.Client", _make_fake_client([], fail=True))

    resp = client.post(f"/v1/mcp/servers/{sid}/reconnect")

    assert resp.status_code == 502
    assert resp.json()["error"]["error"] == "upstream_unavailable"
    row = client.app.state.external_mcp_servers[sid]
    assert row["status"] == "error"
    assert row["error"]
    history = client.app.state.bus._history.get("", [])
    assert any(e.type == "mcp.server.error" for e in history)


def test_reconnect_http_transport(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    sid = _seed_server(client, transport="http")
    monkeypatch.setattr("fastmcp.Client", _make_fake_client(["remote_tool"]))

    resp = client.post(f"/v1/mcp/servers/{sid}/reconnect")

    assert resp.status_code == 200, resp.text
    assert resp.json()["transport"] == "http"
    assert resp.json()["tools"] == ["remote_tool"]


def _seed_server_with_spec(
    client: TestClient,
    spec: dict[str, object],
    sid: str = "mcp_ext_test01",
    *,
    transport: str = "stdio",
) -> str:
    """Seed a registry row with an arbitrary (possibly malformed) spec."""

    client.app.state.external_mcp_servers = {
        sid: {
            "id": sid,
            "name": "everything",
            "status": "error",
            "transport": transport,
            "tools": ["stale"],
            "spec": spec,
            "error": "previous failure",
        }
    }
    return sid


# --- gap-523 hardening: spec validation -----------------------------------


def test_reconnect_stdio_spec_missing_command_returns_422(client: TestClient) -> None:
    """A stored stdio spec with no command is a structured 4xx, not a 500."""

    sid = _seed_server_with_spec(client, {"transport": "stdio", "args": []}, transport="stdio")

    resp = client.post(f"/v1/mcp/servers/{sid}/reconnect")

    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["error"] == "mcp_spec_invalid"
    assert "command" in resp.json()["error"]["message"]


def test_reconnect_stdio_spec_blank_command_returns_422(client: TestClient) -> None:
    """A whitespace-only command is treated as missing (still 4xx)."""

    sid = _seed_server_with_spec(
        client, {"transport": "stdio", "command": "   ", "args": []}, transport="stdio"
    )

    resp = client.post(f"/v1/mcp/servers/{sid}/reconnect")

    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["error"] == "mcp_spec_invalid"


def test_reconnect_http_spec_missing_url_returns_422(client: TestClient) -> None:
    """A stored http spec with no url is a structured 4xx, not a 500."""

    sid = _seed_server_with_spec(client, {"transport": "http"}, transport="http")

    resp = client.post(f"/v1/mcp/servers/{sid}/reconnect")

    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["error"] == "mcp_spec_invalid"
    assert "url" in resp.json()["error"]["message"]


# --- gap-523 hardening: reconnect timeout ----------------------------------


def test_reconnect_timeout_returns_504_and_keeps_registry_intact(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hung probe must not hang the route: it times out into a structured
    504 (mcp_reconnect_timeout), emits mcp.server.error, and leaves the
    registry row in a coherent error state (previous tools preserved, not
    blanked)."""

    sid = _seed_server(client, tools=["known_tool"])
    # Tiny timeout so the hanging probe is cut short immediately.
    monkeypatch.setenv("CLIO_GACT_MCP_RECONNECT_TIMEOUT_S", "0.2")
    monkeypatch.setattr("fastmcp.Client", _make_hanging_client())

    resp = client.post(f"/v1/mcp/servers/{sid}/reconnect")

    assert resp.status_code == 504, resp.text
    body = resp.json()
    assert body["error"]["error"] == "mcp_reconnect_timeout"
    assert body["error"]["details"]["timeout_s"] == pytest.approx(0.2)

    # Registry row left in a coherent error state — NOT half-updated.
    row = client.app.state.external_mcp_servers[sid]
    assert row["status"] == "error"
    assert "timed out" in row["error"]
    # The previously-known tool list is preserved, not blanked by the
    # failed probe.
    assert row["tools"] == ["known_tool"]

    # SSE clients learn about the failure via a global status event.
    history = client.app.state.bus._history.get("", [])
    errors = [e for e in history if e.type == "mcp.server.error"]
    assert len(errors) == 1
    assert errors[0].payload["server_id"] == sid
    assert errors[0].payload["status"] == "error"
    # No success event was emitted on the timeout path.
    assert not any(e.type == "mcp.server.reconnected" for e in history)
