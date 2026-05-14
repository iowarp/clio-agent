"""CLIO-BBBBBBBBBB10: tests for /v1/agents + /v1/catalog/tools.

Exercises the CLIO → GACT translator that exposes the 3 built-in
experts (DataExpert / AnalysisExpert / VisualizationExpert) as
tier-2 AgentDef rows with specialization + keywords populated, and
the flattened tool catalog under /v1/catalog/tools.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from clio_agent.gact.app import build_app


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    # agent=None is fine — /v1/agents doesn't need a live ClioAgent
    # (it reads static Expert.get_capabilities()).
    return TestClient(
        build_app(sessions_path=tmp_path / "sessions.json", agent=None)
    )


def test_list_agents_returns_hierarchy(client: TestClient) -> None:
    resp = client.get("/v1/agents")
    assert resp.status_code == 200
    body = resp.json()
    agents = body["agents"]
    assert isinstance(agents, list)
    # At minimum: 1 tier-1 orchestrator + 3 tier-2 experts.
    tiers = [a["tier"] for a in agents]
    assert 1 in tiers, "expected a tier-1 orchestrator row"
    assert tiers.count(2) >= 3, (
        f"expected ≥3 tier-2 specialists; got tiers={tiers}"
    )


def test_list_agents_tier_filter(client: TestClient) -> None:
    resp = client.get("/v1/agents?tier=2")
    assert resp.status_code == 200
    rows = resp.json()["agents"]
    assert len(rows) >= 3
    for row in rows:
        assert row["tier"] == 2, row
        assert row["specialization"], (
            f"tier-2 agent {row['id']} missing specialization"
        )
        assert row["keywords"], f"tier-2 agent {row['id']} has no keywords"


def test_list_agents_tier_one_only(client: TestClient) -> None:
    resp = client.get("/v1/agents?tier=1")
    assert resp.status_code == 200
    rows = resp.json()["agents"]
    assert len(rows) >= 1
    assert {r["tier"] for r in rows} == {1}
    ids = [r["id"] for r in rows]
    assert "main" in ids, f"tier-1 should include 'main' orchestrator; got {ids}"


def test_list_agents_includes_known_experts(client: TestClient) -> None:
    """Pins the ids CLIO actually exports today. If an expert is
    renamed / removed, this fails — worth catching at the wire
    layer since the TUI's palette hint map keys on these ids."""

    resp = client.get("/v1/agents?tier=2")
    ids = {r["id"] for r in resp.json()["agents"]}
    for expected in {"data", "analysis", "visualization"}:
        assert expected in ids, (
            f"expected tier-2 agent {expected!r}; got {ids}"
        )


def test_list_agents_unknown_tier_returns_empty(client: TestClient) -> None:
    resp = client.get("/v1/agents?tier=99")
    assert resp.status_code == 200
    assert resp.json() == {"agents": []}


def test_catalog_tools_flattens_expert_tools(client: TestClient) -> None:
    resp = client.get("/v1/catalog/tools")
    assert resp.status_code == 200
    tools = resp.json()["tools"]
    assert isinstance(tools, list)
    assert len(tools) > 0
    # Each row has id + name.
    for t in tools:
        assert t["id"] == t["name"]  # same for builtin sources
        assert t["source"] == "builtin"
    # Dedup: no repeats across experts.
    names = [t["name"] for t in tools]
    assert len(names) == len(set(names)), f"expected deduped tools; got {names}"
