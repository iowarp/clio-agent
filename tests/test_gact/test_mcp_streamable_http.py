"""streamable-http external MCP servers must be visible AND callable (#770 C2).

Regression for the transport-construction divergence: a stored external MCP
server spec whose ``transport`` is ``streamable-http`` was accepted by the
install-probe / reconnect / agent-driven-call paths but REJECTED by the
manual tool-call dispatch (``== "http"`` only -> 500 ``unknown stored
transport``), the ``GET /v1/mcp/servers/{sid}/tools`` listing (else ``return
[]``), and the ``/v1/tools`` catalog (else ``continue`` -> the tool vanished).

Folding every construction site onto ``transport_from_spec`` (one canonical
accepted set: stdio | http | streamable-http | sse) makes such a server:

  (a) appear in ``GET /v1/tools``,
  (b) list a non-empty tool set at ``GET /v1/mcp/servers/{sid}/tools``, and
  (c) dispatch a tool call without a 500.

The fastmcp ``Client`` is stubbed so no real network endpoint is needed; we
assert on the transport TYPE handed to ``Client``, not a live round-trip.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from clio_agent.gact.app import build_app


class _FakeTool:
    def __init__(self, name: str) -> None:
        self.name = name
        self.description = f"{name} description"
        self.inputSchema: dict[str, object] = {}
        self.outputSchema: dict[str, object] = {}


def _fake_client_factory(captured: list[object]):
    """Return a stub ``fastmcp.Client`` that records the transport it receives.

    ``captured`` collects each transport object so tests can assert it is a real
    ``StreamableHttpTransport`` (proving the streamable-http branch was taken and
    did not fall through to a reject).
    """

    class _FakeClient:
        def __init__(self, transport: object, **_: object) -> None:
            captured.append(transport)
            self._transport = transport

        async def __aenter__(self) -> "_FakeClient":
            return self

        async def __aexit__(self, *exc: object) -> bool:
            return False

        async def list_tools(self) -> list[_FakeTool]:
            return [_FakeTool("sh_tool")]

        async def call_tool(self, name: str, args: dict[str, object]) -> object:
            return SimpleNamespace(
                content=[SimpleNamespace(type="text", text=f"called {name}")],
                data=None,
                isError=False,
            )

    return _FakeClient


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    return TestClient(build_app(sessions_path=tmp_path / "sessions.json"))


def _seed_streamable_http(client: TestClient, sid: str = "mcp_ext_sh01") -> str:
    client.app.state.external_mcp_servers = {
        sid: {
            "id": sid,
            "name": "remote_sh",
            "status": "ready",
            "transport": "streamable-http",
            # tools left empty so the LIVE-listing transport branch is what is
            # exercised (not the declared-descriptor rows).
            "tools": [],
            "tool_annotations": {"sh_tool": {"readOnlyHint": True}},
            "spec": {"transport": "streamable-http", "url": "https://mcp.example.com/mcp"},
        }
    }
    return sid


def test_streamable_http_tool_visible_in_catalog(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(a) A streamable-http server's live tool must appear in GET /v1/tools."""
    _seed_streamable_http(client)
    captured: list[object] = []
    monkeypatch.setattr("fastmcp.Client", _fake_client_factory(captured))

    resp = client.get("/v1/tools")
    assert resp.status_code == 200, resp.text
    tool_ids = {row.get("id") for row in resp.json()["tools"]}
    assert "sh_tool" in tool_ids, resp.json()["tools"]

    # The streamable-http branch was actually taken (not skipped).
    from fastmcp.client.transports import StreamableHttpTransport

    assert any(isinstance(t, StreamableHttpTransport) for t in captured)


def test_streamable_http_tools_listing_nonempty(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(b) GET /v1/mcp/servers/{sid}/tools must be non-empty for streamable-http."""
    sid = _seed_streamable_http(client)
    captured: list[object] = []
    monkeypatch.setattr("fastmcp.Client", _fake_client_factory(captured))

    resp = client.get(f"/v1/mcp/servers/{sid}/tools")
    assert resp.status_code == 200, resp.text
    names = {t["name"] for t in resp.json()["tools"]}
    assert names == {"sh_tool"}, resp.json()

    from fastmcp.client.transports import StreamableHttpTransport

    assert any(isinstance(t, StreamableHttpTransport) for t in captured)


def test_streamable_http_tool_call_not_500(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(c) POST /v1/mcp/servers/{sid}/call must not 500 on a streamable-http spec."""
    sid = _seed_streamable_http(client)
    captured: list[object] = []
    monkeypatch.setattr("fastmcp.Client", _fake_client_factory(captured))

    resp = client.post(f"/v1/mcp/servers/{sid}/call", json={"tool": "sh_tool", "args": {}})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["tool"] == "sh_tool"
    assert body["is_error"] is False

    from fastmcp.client.transports import StreamableHttpTransport

    assert any(isinstance(t, StreamableHttpTransport) for t in captured)
