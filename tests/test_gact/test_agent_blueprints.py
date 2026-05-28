from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from clio_agent.gact.agent_blueprints import (
    discover_agent_blueprints,
    load_agent_blueprints,
    validate_agent_blueprint_path,
)
from clio_agent.gact.app import build_app


def _write_blueprint(root: Path, blueprint_id: str = "genomics") -> None:
    (root / "experts").mkdir(parents=True)
    root.joinpath("AGENT.md").write_text(
        f"""---
id: {blueprint_id}
version: 0.1.0
title: Genomics Agent
root_expert: root
defaults:
  prompt_profile: heavy
---
Genomics domain agent.
""",
        encoding="utf-8",
    )
    root.joinpath("experts", "root.md").write_text(
        """---
id: root
title: Genomics Root
tier: 1
prompt_id: genomics.root
---
Coordinate genomics work.
""",
        encoding="utf-8",
    )
    root.joinpath("experts", "variant.md").write_text(
        """---
id: variant
title: Variant Expert
parent_id: root
tier: 2
tools:
  - memory_search_sessions
prompt_id: genomics.variant
---
Inspect variant evidence.
""",
        encoding="utf-8",
    )


def test_builtin_agent_blueprint_is_discoverable() -> None:
    blueprints = {row.id: row for row in discover_agent_blueprints()}
    agents = {row.id: row for row in load_agent_blueprints(blueprint_id="data-exploration")}

    assert blueprints["data-exploration"].scope == "builtin"
    assert blueprints["data-exploration"].root_expert == "main"
    assert {"main", "data", "analysis", "visualization", "ndp_catalog"} <= set(agents)
    assert agents["data"].metadata["agent_blueprint_id"] == "data-exploration"


def test_validate_agent_blueprint_markdown_root(tmp_path: Path) -> None:
    root = tmp_path / "genomics"
    _write_blueprint(root)

    body = validate_agent_blueprint_path(root)

    assert body["enabled"] is True
    assert body["agent_blueprint"]["id"] == "genomics"
    rows = {row["id"]: row for row in body["agents"]}
    assert rows["root"]["tier"] == 1
    assert rows["variant"]["parent_id"] == "root"
    assert rows["variant"]["tools"] == ["memory_search_sessions"]


def test_agent_blueprint_activation_replaces_default_agent_graph(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    blueprint = workspace / ".clio" / "agent-blueprints" / "genomics"
    _write_blueprint(blueprint)

    app = build_app(sessions_path=tmp_path / "sessions.json")
    with TestClient(app) as client:
        wid = client.post(
            "/v1/workspaces",
            json={
                "name": "Workspace",
                "root_path": str(workspace),
                "storage_root": str(workspace / ".clio"),
            },
        ).json()["id"]
        sid = client.post(
            "/v1/sessions",
            json={"title": "genomics", "workspace_id": wid},
        ).json()["id"]
        activated = client.post(
            f"/v1/sessions/{sid}/agent-blueprint",
            json={"blueprint_id": "genomics"},
        )
        assert activated.status_code == 200, activated.text
        agents = {
            row["id"]: row
            for row in client.get("/v1/agents", params={"session_id": sid}).json()["agents"]
        }

    assert set(agents) == {"root", "variant"}
    assert "data" not in agents
    assert agents["variant"]["metadata"]["agent_blueprint_id"] == "genomics"


def test_session_agent_overlay_is_session_local(tmp_path: Path) -> None:
    blueprint = tmp_path / "genomics"
    _write_blueprint(blueprint)

    app = build_app(sessions_path=tmp_path / "sessions.json")
    with TestClient(app) as client:
        sid_a = client.post("/v1/sessions", json={"title": "A"}).json()["id"]
        sid_b = client.post("/v1/sessions", json={"title": "B"}).json()["id"]
        for sid in (sid_a, sid_b):
            assert client.post(
                f"/v1/sessions/{sid}/agent-blueprint",
                json={"path": str(blueprint)},
            ).status_code == 200
        saved = client.put(
            f"/v1/sessions/{sid_a}/agent-overlay",
            json={
                "agents": {
                    "variant": {
                        "title": "Session A Variant Expert",
                        "default_model": "gpt-5-mini",
                    }
                }
            },
        )
        assert saved.status_code == 200, saved.text
        agent_a = client.get("/v1/agents/variant", params={"session_id": sid_a}).json()
        agent_b = client.get("/v1/agents/variant", params={"session_id": sid_b}).json()

    assert agent_a["title"] == "Session A Variant Expert"
    assert agent_a["default_model"] == "gpt-5-mini"
    assert agent_a["metadata"]["agent_blueprint_overlay"]["status"] == "applied"
    assert agent_b["title"] == "Variant Expert"
    assert agent_b["default_model"] == ""


def test_agent_blueprint_install_from_local_marketplace(tmp_path: Path) -> None:
    marketplace = tmp_path / "marketplace"
    _write_blueprint(marketplace / "genomics")
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    app = build_app(sessions_path=tmp_path / "sessions.json")
    with TestClient(app) as client:
        wid = client.post(
            "/v1/workspaces",
            json={
                "name": "Workspace",
                "root_path": str(workspace),
                "storage_root": str(workspace / ".clio"),
            },
        ).json()["id"]
        installed = client.post(
            "/v1/agent-blueprints/install",
            json={"source": str(marketplace), "scope": "workspace", "workspace_id": wid},
        )
        assert installed.status_code == 201, installed.text
        listed = client.get("/v1/agent-blueprints", params={"workspace_id": wid}).json()

    ids = {row["id"] for row in listed["agent_blueprints"]}
    assert "genomics" in ids
    assert (workspace / ".clio" / "agent-blueprints" / "genomics" / ".clio-install.md").exists()


def test_agent_blueprint_update_and_delete_installed_blueprint(tmp_path: Path) -> None:
    marketplace = tmp_path / "marketplace"
    _write_blueprint(marketplace / "genomics")
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    app = build_app(sessions_path=tmp_path / "sessions.json")
    with TestClient(app) as client:
        wid = client.post(
            "/v1/workspaces",
            json={
                "name": "Workspace",
                "root_path": str(workspace),
                "storage_root": str(workspace / ".clio"),
            },
        ).json()["id"]
        assert client.post(
            "/v1/agent-blueprints/install",
            json={"source": str(marketplace), "scope": "workspace", "workspace_id": wid},
        ).status_code == 201
        marketplace.joinpath("genomics", "experts", "variant.md").write_text(
            """---
id: variant
title: Updated Variant Expert
parent_id: root
tier: 2
---
Updated behavior.
""",
            encoding="utf-8",
        )
        updated = client.post(
            "/v1/agent-blueprints/genomics/update",
            json={"scope": "workspace", "workspace_id": wid},
        )
        assert updated.status_code == 200, updated.text
        assert "Updated Variant Expert" in (
            workspace / ".clio" / "agent-blueprints" / "genomics" / "experts" / "variant.md"
        ).read_text()
        deleted = client.delete(
            "/v1/agent-blueprints/genomics",
            params={"scope": "workspace", "workspace_id": wid},
        )
        assert deleted.status_code == 200, deleted.text

    assert not (workspace / ".clio" / "agent-blueprints" / "genomics").exists()


def test_agent_blueprint_mcp_descriptor_installs_disabled(tmp_path: Path) -> None:
    root = tmp_path / "marketplace" / "earth"
    _write_blueprint(root, blueprint_id="earth")
    (root / "tools").mkdir()
    root.joinpath("tools", "earthscope.md").write_text(
        """---
id: earthscope
name: EarthScope MCP
transport: stdio
command: earthscope-mcp
args:
  - serve
---
EarthScope descriptor.
""",
        encoding="utf-8",
    )

    body = validate_agent_blueprint_path(root)

    descriptor = body["mcp_descriptors"][0]
    assert descriptor["id"] == "earthscope"
    assert descriptor["enabled"] is False
    assert descriptor["status"] == "disabled"
    assert descriptor["transport"] == "stdio"


def test_agent_blueprint_mcp_descriptor_requires_explicit_enablement(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    root = workspace / ".clio" / "agent-blueprints" / "earth"
    _write_blueprint(root, blueprint_id="earth")
    (root / "tools").mkdir()
    root.joinpath("tools", "earthscope.md").write_text(
        """---
id: earthscope
name: EarthScope MCP
transport: stdio
command: earthscope-mcp
args:
  - serve
---
EarthScope descriptor.
""",
        encoding="utf-8",
    )

    app = build_app(sessions_path=tmp_path / "sessions.json")
    with TestClient(app) as client:
        wid = client.post(
            "/v1/workspaces",
            json={
                "name": "Workspace",
                "root_path": str(workspace),
                "storage_root": str(workspace / ".clio"),
            },
        ).json()["id"]
        rows_before = client.get("/v1/mcp/servers", params={"workspace_id": wid}).json()["servers"]
        enabled = client.post(
            "/v1/agent-blueprints/earth/mcp/earthscope/enable",
            json={"workspace_id": wid},
        )
        assert enabled.status_code == 200, enabled.text
        rows_after = client.get("/v1/mcp/servers", params={"workspace_id": wid}).json()["servers"]

    assert any(
        row.get("source") == "agent_blueprint" and row.get("enabled") is False
        for row in rows_before
    )
    assert enabled.json()["status"] == "enabled_pending_probe"
    assert any(row["id"] == "agent_blueprint_mcp_earth_earthscope" for row in rows_after)
