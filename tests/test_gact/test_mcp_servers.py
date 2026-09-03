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
    """Minimal GACT agent with optional resident MCP inventory state.

    The executor is filed under ``canonical_workspace_root`` because that is the
    key the RUNTIME writes (``ClioAgent.resolve_workspace_tool_executor`` /
    ``lease_workspace_fleet``); a fixture that invents its own key would prove
    the reader agrees with the fixture rather than with the fleet.
    """

    def __init__(self, *, root: str = "", executor: object | None = None) -> None:
        from clio_agent.tools.execution import canonical_workspace_root

        self._workspace_executor_lock = threading.Lock()
        self._workspace_tool_executors = (
            {canonical_workspace_root(root): executor} if root and executor is not None else {}
        )

    def forward(self, question: str, session_id: str) -> None:
        del question, session_id
        raise AssertionError("turn execution is outside this inventory test")


def _ready_executor() -> SimpleNamespace:
    return SimpleNamespace(
        closed=False,
        get_tool_names=lambda: ["ndp_search", "ndp_fetch"],
        namespaces=lambda: ("ndp",),
        is_namespace_prepared=lambda namespace: namespace == "ndp",
    )


def _earthscope_workspace(root: Path) -> None:
    """Write a one-namespace blueprint whose server is never launched."""

    root.joinpath(".clio", "agent-blueprints", "earthscope").mkdir(parents=True)
    root.joinpath(".clio", "agent-blueprints", "earthscope", "AGENT.md").write_text(
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


def _session_rows(client: TestClient, *, root_path: str, workspace: Path) -> dict[str, object]:
    """Activate the blueprint and return the ``/v1/mcp/servers`` body."""

    del workspace
    wid = client.post("/v1/workspaces", json={"name": "Workspace", "root_path": root_path}).json()[
        "id"
    ]
    sid = client.post("/v1/sessions", json={"title": "Earth", "workspace_id": wid}).json()["id"]
    activated = client.post(
        f"/v1/sessions/{sid}/agent-blueprint", json={"blueprint_id": "earthscope"}
    )
    assert activated.status_code == 200, activated.text
    listed = client.get("/v1/mcp/servers", params={"workspace_id": wid, "session_id": sid})
    assert listed.status_code == 200, listed.text
    return listed.json()


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
    _earthscope_workspace(workspace)
    agent = _InventoryAgent(root=str(workspace), executor=_ready_executor())
    app = build_app(sessions_path=tmp_path / "ready-mcp.json", agent=agent)
    with TestClient(app) as session_client:
        body = _session_rows(session_client, root_path=str(workspace), workspace=workspace)

    ndp = next(row for row in body["servers"] if row["name"] == "ndp")
    assert ndp["status"] == "ready"
    assert ndp["tools"] == ["ndp_search", "ndp_fetch"]
    assert body["degradations"] == []


def test_mcp_servers_finds_the_resident_fleet_under_any_root_spelling(tmp_path: Path) -> None:
    """One canonicalizer keys BOTH the fleet registry and the inventory reader.

    ``_runtime_workspace_catalog_cwd`` hands the reader ``str(Path(root).expanduser())``
    while the fleet is keyed off the raw stored ``root_path``. On Windows a
    workspace stored with forward slashes (what a JSON config or the TUI writes)
    makes those two strings differ, so a namespace that is genuinely connected
    reported ``available`` forever -- a wrong answer, silently.
    """

    workspace = tmp_path / "workspace"
    _earthscope_workspace(workspace)
    forward_slashed = workspace.as_posix() + "/"
    assert forward_slashed != str(workspace), "fixture must exercise a different spelling"

    agent = _InventoryAgent(root=forward_slashed, executor=_ready_executor())
    app = build_app(sessions_path=tmp_path / "spelled-mcp.json", agent=agent)
    with TestClient(app) as session_client:
        body = _session_rows(session_client, root_path=forward_slashed, workspace=workspace)

    ndp = next(row for row in body["servers"] if row["name"] == "ndp")
    assert ndp["status"] == "ready"


def test_canonical_workspace_root_collapses_every_spelling_of_one_root(tmp_path: Path) -> None:
    """The canonicalizer itself, so the agreement above cannot drift apart."""

    from clio_agent.tools.execution import canonical_workspace_root

    root = tmp_path / "workspace"
    root.mkdir()
    canonical = canonical_workspace_root(root)
    assert canonical == root.resolve().as_posix()
    for spelling in (
        str(root),
        root.as_posix(),
        root.as_posix() + "/",
        str(root) + "  ",
        str(root / "."),
    ):
        assert canonical_workspace_root(spelling) == canonical, spelling
    # An empty root is "no workspace bound", not a path to normalize.
    assert canonical_workspace_root("") == ""
    assert canonical_workspace_root("   ") == ""


def test_the_fleet_registry_keys_workspace_roots_canonically() -> None:
    """The WRITE side of that agreement: leases file under the canonical key."""

    from clio_agent.agent import ClioAgent
    from clio_agent.tools.execution import canonical_workspace_root

    agent = ClioAgent.__new__(ClioAgent)
    _lock, _executors, leases = agent._workspace_state()
    spelled = "C:/some/workspace/"
    with agent.lease_workspace_fleet(spelled):
        assert list(leases) == [canonical_workspace_root(spelled)]
        assert leases[canonical_workspace_root(spelled)] == 1
    assert leases.get(canonical_workspace_root(spelled), 0) == 0


def test_mcp_servers_names_why_a_session_namespace_has_no_runtime_state(
    tmp_path: Path,
) -> None:
    """An empty snapshot is six different facts; the row must say which one.

    ``workspace_mcp_snapshot`` returned a bare ``{}`` for "no agent", "no fleet
    state", "nothing resident for this root", "the fleet was reaped", and any
    attribute drift alike -- and the row then reported the same ``available`` for
    all of them, so a reaped fleet and an untouched workspace were
    indistinguishable on the Infrastructure page.
    """

    workspace = tmp_path / "workspace"
    _earthscope_workspace(workspace)

    # (a) nothing resident for this root: the workspace was never used.
    idle = build_app(sessions_path=tmp_path / "idle.json", agent=_InventoryAgent())
    with TestClient(idle) as idle_client:
        idle_body = _session_rows(idle_client, root_path=str(workspace), workspace=workspace)
    idle_row = next(row for row in idle_body["servers"] if row["name"] == "ndp")
    assert idle_row["status"] == "available"
    assert idle_row["runtime_unavailable"] == "workspace_fleet_not_started"

    # (b) the fleet WAS resident and got closed (the #933 reaper).
    closed_executor = SimpleNamespace(
        closed=True,
        get_tool_names=lambda: [],
        namespaces=lambda: (),
        is_namespace_prepared=lambda namespace: False,
    )
    reaped = build_app(
        sessions_path=tmp_path / "reaped.json",
        agent=_InventoryAgent(root=str(workspace), executor=closed_executor),
    )
    with TestClient(reaped) as reaped_client:
        reaped_body = _session_rows(reaped_client, root_path=str(workspace), workspace=workspace)
    reaped_row = next(row for row in reaped_body["servers"] if row["name"] == "ndp")
    assert reaped_row["runtime_unavailable"] == "workspace_fleet_closed"

    # (c) an executor missing a required accessor is DRIFT, not "not ready":
    # swallowing it behind a ``lambda: False`` default hid a broken contract.
    drifted = build_app(
        sessions_path=tmp_path / "drifted.json",
        agent=_InventoryAgent(root=str(workspace), executor=SimpleNamespace(closed=False)),
    )
    with TestClient(drifted) as drifted_client:
        drifted_body = _session_rows(drifted_client, root_path=str(workspace), workspace=workspace)
    drifted_row = next(row for row in drifted_body["servers"] if row["name"] == "ndp")
    assert drifted_row["runtime_unavailable"] == "workspace_fleet_interface_drift"
    assert any(
        row["reason"] == "workspace_fleet_interface_drift" for row in drifted_body["degradations"]
    )


def test_mcp_servers_surfaces_an_unreadable_declaration_file(tmp_path: Path) -> None:
    """A declaration that could not be PARSED is not the same as one absent.

    ``load_mcp_servers`` already records unreadable ``mcp.yaml`` paths; the
    inventory dropped those servers from the listing with nothing said, so the
    page showed a shorter list and called it the truth.
    """

    workspace = tmp_path / "workspace"
    _earthscope_workspace(workspace)
    workspace.joinpath(".clio", "mcp.yaml").write_text("mcp_servers: [oops\n", encoding="utf-8")

    app = build_app(sessions_path=tmp_path / "unreadable.json", agent=_InventoryAgent())
    with TestClient(app) as session_client:
        body = _session_rows(session_client, root_path=str(workspace), workspace=workspace)

    reasons = {row["reason"] for row in body["degradations"]}
    assert "mcp_yaml_declaration_unreadable" in reasons
    unreadable = next(
        row for row in body["degradations"] if row["reason"] == "mcp_yaml_declaration_unreadable"
    )
    assert unreadable["detail"]


def test_the_session_inventory_never_takes_the_fleet_lock_on_the_event_loop(
    tmp_path: Path,
) -> None:
    """The snapshot holds ``ClioAgent._workspace_executor_lock``, a threading lock.

    Taking it inline blocks the whole server on whatever turn currently owns the
    fleet, so the read runs on a worker like every other blocking read in this
    branch.
    """

    import asyncio

    workspace = tmp_path / "workspace"
    _earthscope_workspace(workspace)
    loop_threads: list[int] = []

    class _WatchingAgent(_InventoryAgent):
        def __init__(self) -> None:
            super().__init__(root=str(workspace), executor=_ready_executor())
            outer = self

            class _Lock:
                def __enter__(self) -> None:
                    loop_threads.append(threading.get_ident())

                def __exit__(self, *exc: object) -> None:
                    return None

            outer._workspace_executor_lock = _Lock()

    app = build_app(sessions_path=tmp_path / "offloop.json", agent=_WatchingAgent())
    with TestClient(app) as session_client:
        _session_rows(session_client, root_path=str(workspace), workspace=workspace)
        server_loop_thread = session_client.portal.call(  # type: ignore[attr-defined]
            lambda: asyncio.get_running_loop()._thread_id  # type: ignore[attr-defined]
        )

    assert loop_threads, "the snapshot never ran"
    assert server_loop_thread not in loop_threads, "the fleet lock was taken on the event loop"


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
