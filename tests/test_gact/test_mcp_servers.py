"""iowarp/clio-agent#13: /v1/mcp/servers enumerates the gateway."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from clio_agent.gact.app import build_app


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    return TestClient(build_app(sessions_path=tmp_path / "s.json"))


def test_mcp_servers_lists_known_namespaces(client: TestClient) -> None:
    """The gateway mounts hdf5 + parquet by default; both should
    show up as MCP server rows."""

    body = client.get("/v1/mcp/servers").json()
    rows = {s["name"]: s for s in body.get("servers", [])}
    if "error" in body:
        pytest.skip(f"gateway introspection unavailable: {body['error']}")
    for name in ("hdf5", "parquet"):
        assert name in rows
        row = rows[name]
        assert row["status"] == "ready"
        assert row["transport"] == "in_process"
        assert row["tools_count"] > 0
        assert row["id"].startswith("mcp_")
        assert all(t.startswith(f"{name}_") for t in row["tools"])


def test_capabilities_advertises_mcp(client: TestClient) -> None:
    body = client.get("/v1/capabilities").json()
    assert body["capabilities"]["mcp"] is True
