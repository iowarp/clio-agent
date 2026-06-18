"""GET /v1/mcp/handshake — live per-server readiness probe for the TUI.

Complements ``/v1/mcp/servers`` (the mounted catalog): this endpoint reports,
per DECLARED MCP server, whether it is reachable and how many tools it exposes,
so a client can show "clio-kit up (12 tools), hdf5 down". The actual subprocess
spawn lives in ``handshake_mcp_servers`` (covered in tests/test_providers); here
we pin the endpoint's wire shape against mocked reports — no real servers spawned.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from clio_agent.gact.app import build_app
from clio_agent.providers.handshake.mcp import MCPServerReport
from clio_agent.providers.handshake.model import ConnectivityState


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    return TestClient(build_app(sessions_path=tmp_path / "s.json"))


def test_mcp_handshake_reports_per_server_status(client: TestClient, monkeypatch) -> None:
    """One reachable + one down server: both surface, with state/tools/error."""
    reports = [
        MCPServerReport(
            name="clio-kit",
            connectivity=ConnectivityState.OK,
            transport="stdio",
            tool_count=2,
            tools=("geo_filter_points_by_radius", "plot_plot_timeseries"),
            latency_ms=12.5,
        ),
        MCPServerReport(
            name="hdf5",
            connectivity=ConnectivityState.UNREACHABLE,
            transport="stdio",
            error="spawn failed: uvx not found",
        ),
    ]

    async def _fake_probe(specs, **kwargs):  # noqa: ANN001 - test double
        return reports

    monkeypatch.setattr("clio_agent.providers.handshake.handshake_mcp_servers", _fake_probe)

    body = client.get("/v1/mcp/handshake").json()
    rows = {s["name"]: s for s in body["servers"]}

    assert rows["clio-kit"]["reachable"] is True
    assert rows["clio-kit"]["state"] == "ready"
    assert rows["clio-kit"]["tools_count"] == 2
    assert "plot_plot_timeseries" in rows["clio-kit"]["tools"]

    # A down server surfaces as unavailable with its error, never sinking the rest.
    assert rows["hdf5"]["reachable"] is False
    assert rows["hdf5"]["state"] == "unavailable"
    assert "uvx not found" in rows["hdf5"]["error"]


def test_mcp_handshake_empty_declared_set(client: TestClient, monkeypatch) -> None:
    """No declared servers -> an empty list, not an error."""

    async def _none(specs, **kwargs):  # noqa: ANN001 - test double
        return []

    monkeypatch.setattr("clio_agent.providers.handshake.handshake_mcp_servers", _none)

    body = client.get("/v1/mcp/handshake").json()
    assert body == {"servers": []}
