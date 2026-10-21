"""tests for /v1/agents + /v1/catalog/tools + /v1/tools.

``GET /v1/agents`` lists the installed agents (from the trusted marketplaces of
the plugin store) and the user agents saved as a parent plus a delta. Every
test app installs the hermetic test marketplace (``tests/fixtures/
plugin_marketplace``) as the default ``clio`` marketplace, so the listing is
exactly ``clio/standard`` plus whatever a test saves. The plugin/agent route
family is covered in depth by ``tests/test_gact/test_plugin_routes.py``.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from clio_agent.gact.app import build_app
from tests._plugin_fixtures import custom_marketplace


@pytest.fixture()
def client(tmp_path: Path) -> Iterator[TestClient]:
    # agent=None is fine; /v1/agents reads the plugin store, not a live ClioAgent.
    with TestClient(build_app(sessions_path=tmp_path / "sessions.json", agent=None)) as started:
        yield started


def test_list_agents_is_the_installed_default_marketplace(client: TestClient) -> None:
    resp = client.get("/v1/agents")
    assert resp.status_code == 200
    agents = resp.json()["agents"]
    assert [row["ref"] for row in agents] == ["clio/standard"]
    standard = agents[0]
    assert standard["id"] == "standard"
    assert standard["kind"] == "agent"
    assert standard["marketplace"] == "clio"
    assert standard["flow"]
    assert standard["spec_checksum"]
    selected = [row["ref"] for row in standard["plugins"]]
    assert {"workspace", "shell", "visuals", "delegation", "document-runtime"} <= {
        ref.split("@", 1)[0].rsplit("/", 1)[-1] for ref in selected
    }


def test_list_agents_includes_every_agent_of_the_default_marketplace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    custom_marketplace(
        tmp_path,
        monkeypatch,
        agents={
            "reviewer.py": """
            from clio_agent.sdk import Agent


            class Reviewer(Agent):
                id = "reviewer"
                version = "2.1.0"
                name = "Reviewer"
                role = "a code reviewer"
                description = "Reviews diffs."
                plugins = Agent.plugins + ["workspace"]
            """
        },
    )
    with TestClient(build_app(sessions_path=tmp_path / "s.json", agent=None)) as local:
        rows = {row["ref"]: row for row in local.get("/v1/agents").json()["agents"]}
    assert set(rows) == {"clio/reviewer", "clio/standard"}
    assert rows["clio/reviewer"]["name"] == "Reviewer"
    assert rows["clio/reviewer"]["version"] == "2.1.0"
    assert rows["clio/reviewer"]["description"] == "Reviews diffs."


def test_list_agents_includes_user_agents_as_parent_plus_delta(client: TestClient) -> None:
    saved = client.post(
        "/v1/agents/user",
        json={"id": "no-shell", "extends": "clio/standard", "remove": ["shell"]},
    )
    assert saved.status_code == 201, saved.text

    rows = {row["ref"]: row for row in client.get("/v1/agents").json()["agents"]}
    assert set(rows) == {"clio/standard", "user/no-shell"}
    user = rows["user/no-shell"]
    assert user["kind"] == "user"
    assert user["marketplace"] == "user"
    assert user["extends"] == "clio/standard@^1"
    assert user["delta"] == {"add": [], "remove": ["shell"]}


def test_list_agents_does_not_import_scientific_tool_servers(client: TestClient) -> None:
    sys.modules.pop("clio_agent.tools.servers.hdf5_server", None)
    sys.modules.pop("h5py", None)
    resp = client.get("/v1/agents")
    assert resp.status_code == 200
    assert "clio_agent.tools.servers.hdf5_server" not in sys.modules
    assert "h5py" not in sys.modules


def test_catalog_tools_describes_the_claimable_builtin_surface(
    client: TestClient,
) -> None:
    """The session-less catalog describes every built-in tool a plugin may claim."""

    resp = client.get("/v1/catalog/tools")
    assert resp.status_code == 200
    rows = resp.json()["tools"]
    names = {row["name"] for row in rows}
    assert {
        "fs_read_file",
        "create_artifact",
        "query_tasks",
        "observe_tasks",
        "send_agent_message",
    } <= names
    # Claimable by a plugin even though the test standard agent does not claim it.
    assert "memory_search_sessions" in names
    assert {
        "spawn_agent_task",
        "query_tasks",
        "observe_tasks",
        "wait_tasks",
        "cancel_tasks",
        "get_task_result",
    } <= names
    # The legacy task aliases are claimable built-ins too (a plugin opts in with
    # ``native_tool``), so the catalog describes them; no agent gets them implicitly.
    assert {"observe_agent_tasks", "wait_agent_tasks", "get_agent_task_output"} <= names

    # #1350: every row carries a typed input/output schema and a declared
    # domain -- the desktop Tools view groups/filters by these instead of a
    # client-side name regex.
    by_name = {row["name"]: row for row in rows}
    for name in ("fs_read_file", "create_artifact", "spawn_agent_task"):
        row = by_name[name]
        assert row["input_schema"]["type"] == "object"
        assert row["output_schema"]
        assert row["domain"]
    assert by_name["fs_read_file"]["domain"] == "workspace"
    assert by_name["create_artifact"]["domain"] == "artifacts"


def test_unified_tools_endpoint_exposes_inspector_metadata(client: TestClient) -> None:
    resp = client.get("/v1/tools")
    assert resp.status_code == 200
    by_name = {tool["name"]: tool for tool in resp.json()["tools"]}

    shell = by_name["shell_bash"]
    assert shell["owner"] == "utility"
    assert "chat" in shell["visible_to"]
    assert "diagnostic" in shell["tags"]

    detail = client.get("/v1/tools/shell_bash")
    assert detail.status_code == 200
    body = detail.json()
    assert body["owner"] == "utility"
    assert "chat" in body["visible_to"]
    assert body["input_schema"]


def test_unified_tools_endpoint_includes_preloaded_agent_runtime_mcps(
    client: TestClient,
) -> None:
    """Workspace MCP tools advertised to the model also appear in the live catalog."""

    relay_tool = SimpleNamespace(
        name="relay_jarvis_run",
        title="Run distributed workflow",
        description="Run a durable JARVIS pipeline.",
        inputSchema={"type": "object", "required": ["pipeline_id"]},
        outputSchema={"type": "object"},
    )

    class RuntimeExecutor:
        def get_all_tool_definitions(self) -> dict[str, object]:
            return {
                "relay_jarvis_run": relay_tool,
                # A duplicate runtime definition must not duplicate a row that
                # the bundled gateway already advertised.
                "shell_bash": SimpleNamespace(name="shell_bash"),
            }

    app = cast(FastAPI, client.app)
    app.state.agent = SimpleNamespace(tool_executor=RuntimeExecutor())

    response = client.get("/v1/tools")
    assert response.status_code == 200
    tools = response.json()["tools"]
    runtime = next(row for row in tools if row["name"] == "relay_jarvis_run")
    assert runtime["id"] == "relay_jarvis_run"
    assert runtime["title"] == "Run distributed workflow"
    assert runtime["description"] == "Run a durable JARVIS pipeline."
    assert runtime["server_id"] == "mcp_relay"
    assert runtime["source"] == "agent_runtime_mcp"
    assert runtime["input_schema"] == {
        "type": "object",
        "required": ["pipeline_id"],
    }
    assert runtime["output_schema"] == {"type": "object"}
    assert sum(row["name"] == "shell_bash" for row in tools) == 1

    detail = client.get("/v1/tools/relay_jarvis_run")
    assert detail.status_code == 200
    assert detail.json() == runtime
