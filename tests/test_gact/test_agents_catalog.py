"""tests for /v1/agents + /v1/catalog/tools.

Exercises the CLIO to GACT translator that exposes registry-loaded Agent
Blueprint experts and dynamic skill/user agents as AgentDef rows, plus the
flattened tool catalog under /v1/catalog/tools.

Blueprint activation is EXPLICIT only (owner ruling, 2026-08-05, commit
aa906022): a session that activated no blueprint resolves no blueprint, and
runs the code-shipped builtin ``main`` (``catalog._builtin_main_agent``)
instead of any installed-but-unactivated Agent Blueprint snapshot -- including
the ``DEFAULT_AGENT_BLUEPRINT_ID`` registry default the autouse
``allow_pytest_tmp_path`` fixture installs on disk for every test. The
multi-tier hierarchy / capability-ref / tool-flattening mechanism these tests
exercise therefore needs an EXPLICIT ``POST /v1/sessions/{sid}/agent-blueprint``
activation to have any rows to show; the bare/no-activation contract itself is
covered by ``tests/test_gact/test_agent_resolution_unify.py``.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from clio_agent.gact.agent_blueprints import DEFAULT_AGENT_BLUEPRINT_ID
from clio_agent.gact.app import build_app


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    # agent=None is fine; /v1/agents doesn't need a live ClioAgent
    # (it reads static Expert.get_capabilities()).
    return TestClient(build_app(sessions_path=tmp_path / "sessions.json", agent=None))


def _activated_session(client: TestClient) -> str:
    """Create a session and EXPLICITLY activate the installed default-registry
    blueprint fixture (main/data/analysis/visualization/ndp_catalog/sac_format/
    utility -- written to the isolated per-test config root by the autouse
    ``allow_pytest_tmp_path`` fixture), so ``/v1/agents``/``/v1/catalog/tools``
    have a real, explicitly-bound multi-tier hierarchy to serve."""

    sid = client.post("/v1/sessions", json={"title": "activated"}).json()["id"]
    resp = client.post(
        f"/v1/sessions/{sid}/agent-blueprint",
        json={"blueprint_id": DEFAULT_AGENT_BLUEPRINT_ID},
    )
    assert resp.status_code == 200, resp.text
    return sid


def test_bare_session_agents_is_just_the_builtin_main(client: TestClient) -> None:
    """A session with NO explicit activation resolves ONLY the builtin main.

    Regression coverage for the implicit expert-pack selection this file used
    to (accidentally) rely on: before the fix, ``GET /v1/agents`` with no
    session activation silently surfaced the installed default-registry
    snapshot's full hierarchy (main/data/analysis/...). Now it resolves the
    code-shipped ``main`` only -- the same agent a bare turn actually executes.
    """

    resp = client.get("/v1/agents")
    assert resp.status_code == 200
    agents = resp.json()["agents"]
    assert [row["id"] for row in agents] == ["main"]
    assert agents[0]["source"] == "builtin"
    assert agents[0]["metadata"].get("definition_kind") == "builtin_main"


def test_list_agents_returns_hierarchy(client: TestClient) -> None:
    sid = _activated_session(client)
    resp = client.get("/v1/agents", params={"session_id": sid})
    assert resp.status_code == 200
    body = resp.json()
    agents = body["agents"]
    assert isinstance(agents, list)
    # At minimum: 1 tier-1 orchestrator + built-in tier-2 specialists.
    tiers = [a["tier"] for a in agents]
    assert 1 in tiers, "expected a tier-1 orchestrator row"
    assert tiers.count(2) >= 4, f"expected >=4 tier-2 specialists; got tiers={tiers}"
    assert tiers.count(3) >= 2, f"expected nested tier-3 specialists; got tiers={tiers}"


def test_list_agents_does_not_import_scientific_tool_servers(client: TestClient) -> None:
    sys.modules.pop("clio_agent.tools.servers.hdf5_server", None)
    sys.modules.pop("h5py", None)
    sid = _activated_session(client)
    resp = client.get("/v1/agents", params={"session_id": sid})
    assert resp.status_code == 200
    assert "clio_agent.tools.servers.hdf5_server" not in sys.modules
    assert "h5py" not in sys.modules


def test_list_agents_tier_filter(client: TestClient) -> None:
    sid = _activated_session(client)
    resp = client.get("/v1/agents", params={"session_id": sid, "tier": 2})
    assert resp.status_code == 200
    rows = resp.json()["agents"]
    assert len(rows) >= 4
    for row in rows:
        assert row["tier"] == 2, row
        assert row["keywords"], f"tier-2 agent {row['id']} has no keywords"
        if row["metadata"].get("agent_blueprint_id"):
            assert row["metadata"]["definition_kind"] == "agent_blueprint"
        else:
            assert row["specialization"], f"tier-2 dynamic agent {row['id']} missing specialization"


def test_list_agents_tier_one_only(client: TestClient) -> None:
    sid = _activated_session(client)
    resp = client.get("/v1/agents", params={"session_id": sid, "tier": 1})
    assert resp.status_code == 200
    rows = resp.json()["agents"]
    assert len(rows) >= 1
    assert {r["tier"] for r in rows} == {1}
    ids = [r["id"] for r in rows]
    assert "main" in ids, f"tier-1 should include 'main' orchestrator; got {ids}"


def test_bare_session_tier_one_is_the_builtin_main(client: TestClient) -> None:
    """Without activation, the ONLY tier-1 row is the code-shipped builtin main."""

    resp = client.get("/v1/agents", params={"tier": 1})
    assert resp.status_code == 200
    rows = resp.json()["agents"]
    assert [r["id"] for r in rows] == ["main"]
    assert rows[0]["source"] == "builtin"


def test_list_agents_tier_three_nested_science_experts(client: TestClient) -> None:
    sid = _activated_session(client)
    resp = client.get("/v1/agents", params={"session_id": sid, "tier": 3})
    assert resp.status_code == 200
    rows = resp.json()["agents"]
    by_id = {row["id"]: row for row in rows}

    assert {"ndp_catalog", "sac_format"} <= set(by_id)
    assert by_id["ndp_catalog"]["parent_id"] == "data"
    assert "ndp_search_datasets" in by_id["ndp_catalog"]["tools"]
    assert by_id["sac_format"]["parent_id"] == "analysis"
    assert "sac_compute_trace_statistics" in by_id["sac_format"]["tools"]


def test_list_agents_includes_known_experts(client: TestClient) -> None:
    """Pins the ids CLIO actually exports for an EXPLICITLY activated default
    registry blueprint. If an expert is renamed / removed, this fails; worth
    catching at the wire layer since the TUI's palette hint map keys on these
    ids."""

    sid = _activated_session(client)
    resp = client.get("/v1/agents", params={"session_id": sid, "tier": 2})
    ids = {r["id"] for r in resp.json()["agents"]}
    for expected in {"data", "analysis", "visualization"}:
        assert expected in ids, f"expected tier-2 agent {expected!r}; got {ids}"


def test_agents_expose_normalized_capability_refs(client: TestClient) -> None:
    sid = _activated_session(client)
    resp = client.get("/v1/agents", params={"session_id": sid})
    assert resp.status_code == 200
    by_id = {row["id"]: row for row in resp.json()["agents"]}

    main_refs = {(ref["kind"], ref["id"]): ref for ref in by_id["main"]["capability_refs"]}
    assert ("command", "/clear") in main_refs
    assert main_refs[("command", "/optimize")]["status"] == "unavailable"
    # #801: uniform structured not-implemented reason code across surfaces.
    assert main_refs[("command", "/optimize")]["metadata"]["error"] == "optimizer_not_implemented"
    assert "/cache-stats" in by_id["main"]["commands"]

    data_refs = {(ref["kind"], ref["id"]): ref for ref in by_id["data"]["capability_refs"]}
    assert ("tool", "hdf5_analyze_dataset") in data_refs
    assert data_refs[("tool", "hdf5_analyze_dataset")]["status"] == "available"


def test_list_agents_unknown_tier_returns_empty(client: TestClient) -> None:
    sid = _activated_session(client)
    resp = client.get("/v1/agents", params={"session_id": sid, "tier": 99})
    assert resp.status_code == 200
    assert resp.json() == {"agents": []}


def test_catalog_tools_is_empty_with_no_blueprint_installed_or_activated(
    client: TestClient,
) -> None:
    """``/v1/catalog/tools`` has no session concept (the route takes no
    session_id) -- it can only ever reflect the code-shipped catalog, never a
    particular session's activation. ``catalog._builtin_tools()`` flattens
    tier-2/3 CURATED per-expert tool lists (RULE 5); the ONE code-shipped agent
    (``catalog._builtin_main_agent``) is tier 1 with the raw native tool
    surface, not a curated sub-expert list, so there is nothing to flatten.
    """

    resp = client.get("/v1/catalog/tools")
    assert resp.status_code == 200
    assert resp.json()["tools"] == []


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

    client.app.state.agent = SimpleNamespace(tool_executor=RuntimeExecutor())

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
