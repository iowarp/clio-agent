"""iowarp/clio-agent#13: /v1/mcp/servers enumerates the gateway."""

from __future__ import annotations

import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from fastmcp import FastMCP

from clio_agent.gact.app import build_app


class _InventoryAgent:
    """Minimal GACT agent with optional resident MCP inventory state."""

    def __init__(self, *, root: str = "", executor: object | None = None) -> None:
        self._workspace_executor_lock = threading.Lock()
        self._workspace_tool_executors = {root: executor} if root and executor is not None else {}

    def forward(self, question: str, session_id: str) -> None:
        del question, session_id
        raise AssertionError("turn execution is outside this inventory test")


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


def test_mcp_servers_projects_selected_session_blueprint_without_launching(tmp_path: Path) -> None:
    """Session inventory includes declared MCPs before a namespace is first used."""

    workspace = tmp_path / "workspace"
    blueprint = workspace / ".clio" / "agent-blueprints" / "earthscope"
    (blueprint / "experts").mkdir(parents=True)
    blueprint.joinpath("AGENT.md").write_text(
        """---
id: earthscope
version: 0.1.0
title: EarthScope Skills
root_expert: investigator
mcp_servers:
  ndp: never-launch-ndp serve
  geo: never-launch-geo serve
---
Investigate EarthScope data.
""",
        encoding="utf-8",
    )
    blueprint.joinpath("experts", "investigator.md").write_text(
        """---
id: investigator
title: Investigator
tier: 1
---
Investigate.
""",
        encoding="utf-8",
    )
    app = build_app(sessions_path=tmp_path / "session-mcp.json", agent=_InventoryAgent())
    with TestClient(app) as session_client:
        wid = session_client.post(
            "/v1/workspaces",
            json={"name": "Workspace", "root_path": str(workspace)},
        ).json()["id"]
        sid = session_client.post(
            "/v1/sessions", json={"title": "Earth", "workspace_id": wid}
        ).json()["id"]
        activated = session_client.post(
            f"/v1/sessions/{sid}/agent-blueprint", json={"blueprint_id": "earthscope"}
        )
        assert activated.status_code == 200, activated.text
        response = session_client.get(
            "/v1/mcp/servers", params={"workspace_id": wid, "session_id": sid}
        )

    assert response.status_code == 200, response.text
    rows = {row["name"]: row for row in response.json()["servers"]}
    assert rows["ndp"]["status"] == "available"
    assert rows["ndp"]["source"] == "agent_blueprint"
    assert rows["ndp"]["agent_blueprint_id"] == "earthscope"
    assert rows["ndp"]["agent_blueprint_name"] == "EarthScope Skills"
    assert rows["ndp"]["session_id"] == sid
    assert rows["geo"]["status"] == "available"
    assert rows["ndp"]["tools_count"] == 0


def test_mcp_servers_reports_resident_session_namespace_as_ready(tmp_path: Path) -> None:
    """A connected persistent workspace namespace is reported as ready."""

    workspace = tmp_path / "workspace"
    workspace.joinpath(".clio", "agent-blueprints", "earthscope").mkdir(parents=True)
    blueprint = workspace / ".clio" / "agent-blueprints" / "earthscope" / "AGENT.md"
    blueprint.write_text(
        """---
id: earthscope
version: 0.1.0
title: EarthScope Skills
mcp_servers:
  ndp: never-launch-ndp serve
---
Investigate EarthScope data.
""",
        encoding="utf-8",
    )
    executor = SimpleNamespace(
        closed=False,
        get_tool_names=lambda: ["ndp_search", "ndp_fetch"],
        namespaces=lambda: ("ndp",),
        is_namespace_prepared=lambda namespace: namespace == "ndp",
    )
    agent = _InventoryAgent(root=str(workspace), executor=executor)
    app = build_app(sessions_path=tmp_path / "ready-mcp.json", agent=agent)
    with TestClient(app) as session_client:
        wid = session_client.post(
            "/v1/workspaces", json={"name": "Workspace", "root_path": str(workspace)}
        ).json()["id"]
        sid = session_client.post(
            "/v1/sessions", json={"title": "Earth", "workspace_id": wid}
        ).json()["id"]
        activated = session_client.post(
            f"/v1/sessions/{sid}/agent-blueprint", json={"blueprint_id": "earthscope"}
        )
        assert activated.status_code == 200, activated.text
        rows = session_client.get(
            "/v1/mcp/servers", params={"workspace_id": wid, "session_id": sid}
        ).json()["servers"]

    ndp = next(row for row in rows if row["name"] == "ndp")
    assert ndp["status"] == "ready"
    assert ndp["tools"] == ["ndp_search", "ndp_fetch"]


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


def test_stdio_install_retains_environment_and_redacts_response(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A local provider keeps launch settings for reconnect without disclosing values."""
    server = FastMCP("environment-test")
    captured_specs: list[dict[str, object]] = []

    @server.tool(name="ping")
    def ping() -> str:
        return "pong"

    def capture_transport(spec: dict[str, object]) -> FastMCP:
        captured_specs.append(spec)
        return server

    app = build_app(sessions_path=tmp_path / "environment.json")
    monkeypatch.setattr("clio_agent.gact.routes.mcp.transport_from_spec", capture_transport)
    with TestClient(app) as environment_client:
        response = environment_client.post(
            "/v1/mcp/servers",
            json={
                "name": "Web search",
                "transport": "stdio",
                "command": "web-tools",
                "args": ["serve"],
                "env": {"WEB_STATE_DIR": str(tmp_path / "web-state")},
            },
        )

        assert response.status_code == 201, response.text
        sid = response.json()["id"]
        stored_spec = environment_client.app.state.external_mcp_servers[sid]["spec"]

    expected_env = {"WEB_STATE_DIR": str(tmp_path / "web-state")}
    assert captured_specs == [
        {
            "transport": "stdio",
            "command": "web-tools",
            "args": ["serve"],
            "env": expected_env,
        }
    ]
    assert stored_spec["env"] == expected_env
    assert response.json()["spec"]["env"] == {"WEB_STATE_DIR": "<redacted>"}
