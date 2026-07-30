"""iowarp/clio-agent#13: /v1/mcp/servers enumerates the gateway."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from fastmcp import FastMCP

from clio_agent.gact.app import build_app


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    return TestClient(build_app(sessions_path=tmp_path / "s.json"))


def test_mcp_servers_lists_known_namespaces(client: TestClient) -> None:
    """The gateway mounts the universal built-ins (fs + shell) by default;
    both should show up as MCP server rows. Domain servers are declared MCPs,
    not bundled in core."""

    body = client.get("/v1/mcp/servers").json()
    rows = {s["name"]: s for s in body.get("servers", [])}
    # The gateway is in-process here, so introspection must succeed: fs + shell
    # are always mounted. (A gateway failure lands as a ``status: "error"`` row,
    # so the assertions below surface it rather than the run silently skipping.)
    assert "fs" in rows
    assert "shell" in rows
    for name in ("fs", "shell"):
        row = rows[name]
        assert row["status"] == "ready"
        assert row["transport"] == "in_process"
        assert row["tools_count"] > 0
        assert row["id"].startswith("mcp_")
        assert all(t.startswith(f"{name}_") for t in row["tools"])


def test_capabilities_advertises_mcp(client: TestClient) -> None:
    body = client.get("/v1/capabilities").json()
    assert body["capabilities"]["mcp"] is True


def test_mcp_prompt_get_round_trips_arguments_and_messages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An external in-memory prompt honors arguments and returns parsed messages."""
    server = FastMCP("prompt-test")

    @server.prompt(name="welcome")
    def welcome_prompt(person: str, punctuation: str) -> str:
        return f"Welcome, {person}{punctuation}"

    app = build_app(sessions_path=tmp_path / "prompts.json")
    sid = "mcp_ext_prompt"
    with TestClient(app) as prompt_client:
        prompt_client.app.state.external_mcp_servers = {
            sid: {"spec": {"transport": "stdio", "command": "unused"}}
        }
        monkeypatch.setattr("clio_agent.gact.routes.mcp.transport_from_spec", lambda _spec: server)
        response = prompt_client.post(
            f"/v1/mcp/servers/{sid}/prompts/get",
            json={
                "name": "welcome",
                "arguments": {"person": "Alice", "punctuation": "!"},
            },
        )

    assert response.status_code == 200, response.text
    assert response.json()["prompt"]["messages"] == [
        {"role": "user", "content": {"type": "text", "text": "Welcome, Alice!"}}
    ]
