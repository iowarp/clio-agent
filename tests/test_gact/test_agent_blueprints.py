from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from clio_agent.gact.agent_blueprints import (
    discover_agent_blueprints,
    load_agent_blueprints,
    validate_agent_blueprint_path,
)
from clio_agent.gact.app import build_app
from tests.test_gact.conftest import complete_turn


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


def _write_data_root_blueprint(root: Path, blueprint_id: str = "remote-data") -> None:
    (root / "experts").mkdir(parents=True)
    root.joinpath("AGENT.md").write_text(
        f"""---
id: {blueprint_id}
version: 0.1.0
title: Remote Data Agent
default_expert: data
---
Remote marketplace agent.
""",
        encoding="utf-8",
    )
    root.joinpath("experts", "data.md").write_text(
        """---
id: data
title: Remote Data Orchestrator
tier: 1
prompt_profile: heavy
---
REMOTE BLUEPRINT ORCHESTRATOR MARKER.
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

    app = build_app(sessions_path=tmp_path / "sessions.json", agent=SimpleNamespace())
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

    app = build_app(sessions_path=tmp_path / "sessions.json", agent=SimpleNamespace())
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


def test_marketplace_install_supports_distinct_session_blueprints(tmp_path: Path) -> None:
    marketplace = tmp_path / "marketplace"
    _write_blueprint(marketplace / "genomics-review", blueprint_id="genomics-review")
    _write_data_root_blueprint(
        marketplace / "materials-crystal-review",
        blueprint_id="materials-crystal-review",
    )
    workspace = tmp_path / "workspace"
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
        install = client.post(
            "/v1/agent-blueprints/install",
            json={"source": str(marketplace), "scope": "workspace", "workspace_id": wid},
        )
        assert install.status_code == 201, install.text
        installed = {row["id"] for row in install.json()["installed"]}
        assert installed == {"genomics-review", "materials-crystal-review"}

        sid_genomics = client.post(
            "/v1/sessions",
            json={"title": "genomics", "workspace_id": wid},
        ).json()["id"]
        sid_materials = client.post(
            "/v1/sessions",
            json={"title": "materials", "workspace_id": wid},
        ).json()["id"]
        assert client.post(
            f"/v1/sessions/{sid_genomics}/agent-blueprint",
            json={"blueprint_id": "genomics-review"},
        ).status_code == 200
        assert client.post(
            f"/v1/sessions/{sid_materials}/agent-blueprint",
            json={"blueprint_id": "materials-crystal-review"},
        ).status_code == 200

        genomics = client.get(f"/v1/sessions/{sid_genomics}/agent-blueprint").json()
        materials = client.get(f"/v1/sessions/{sid_materials}/agent-blueprint").json()
        genomics_agents = client.get(
            "/v1/agents",
            params={"session_id": sid_genomics},
        ).json()["agents"]
        materials_agents = client.get(
            "/v1/agents",
            params={"session_id": sid_materials},
        ).json()["agents"]

    assert genomics["active_agent_blueprint_id"] == "genomics-review"
    assert materials["active_agent_blueprint_id"] == "materials-crystal-review"
    assert genomics["activation"]["active_agent_blueprint_source"] == str(marketplace)
    assert genomics["activation"]["active_agent_blueprint_source_kind"] == "path"
    assert genomics["activation"]["active_agent_blueprint_checksum"]
    assert genomics["activation"]["active_agent_blueprint_installed_at"]
    assert materials["activation"]["active_agent_blueprint_source"] == str(marketplace)
    assert materials["activation"]["active_agent_blueprint_checksum"]
    assert {row["id"] for row in genomics_agents} == {"root", "variant"}
    assert {row["id"] for row in materials_agents} == {"data"}


def test_agent_blueprint_install_from_git_marketplace_records_pinned_metadata(
    tmp_path: Path,
) -> None:
    marketplace = tmp_path / "marketplace"
    _write_blueprint(marketplace / "genomics")
    subprocess.run(["git", "init"], cwd=marketplace, check=True, stdout=subprocess.PIPE)
    subprocess.run(["git", "add", "."], cwd=marketplace, check=True, stdout=subprocess.PIPE)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=CLIO Test",
            "-c",
            "user.email=clio@example.invalid",
            "commit",
            "-m",
            "Add genomics blueprint",
        ],
        cwd=marketplace,
        check=True,
        stdout=subprocess.PIPE,
    )
    commit = subprocess.check_output(
        ["git", "-C", str(marketplace), "rev-parse", "HEAD"],
        text=True,
    ).strip()
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
            json={
                "source": marketplace.as_uri(),
                "scope": "workspace",
                "workspace_id": wid,
                "blueprint_id": "genomics",
            },
        )

    assert installed.status_code == 201, installed.text
    metadata = installed.json()["installed"][0]["install"]
    assert metadata["source_kind"] == "git"
    assert metadata["source"] == marketplace.as_uri()
    assert metadata["commit"] == commit
    assert metadata["checksum"]
    assert metadata["installed_at"]


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


def test_active_agent_blueprint_drives_turn_runtime_and_overrides_builtin_ids(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace = tmp_path / "workspace"
    blueprint = workspace / ".clio" / "agent-blueprints" / "remote-data"
    _write_data_root_blueprint(blueprint)
    calls: list[dict[str, str]] = []

    async def no_stream(*args, **kwargs):
        return None

    def fake_prompt_runner(base_agent, agent_def, question, session_id, cancel_requested=None):
        del base_agent, cancel_requested
        calls.append(
            {
                "agent_id": agent_def.id,
                "title": agent_def.title,
                "system_prompt": agent_def.system_prompt,
                "question": question,
                "session_id": session_id,
            }
        )
        return SimpleNamespace(
            answer=f"runtime from {agent_def.id}",
            selected_expert=agent_def.id,
            routing_rationale="session blueprint",
            route_source="agent_blueprint",
            error_info=None,
        )

    monkeypatch.setattr("clio_agent.gact.app._try_streamed_forward", no_stream)
    monkeypatch.setattr("clio_agent.gact.app._run_prompt_user_agent", fake_prompt_runner)

    app = build_app(sessions_path=tmp_path / "sessions.json", agent=SimpleNamespace())
    with TestClient(app) as client:
        wid = client.post(
            "/v1/workspaces",
            json={
                "name": "Workspace",
                "root_path": str(workspace),
                "storage_root": str(workspace / ".clio"),
            },
        ).json()["id"]
        sid_blueprint = client.post(
            "/v1/sessions",
            json={"title": "blueprint", "workspace_id": wid},
        ).json()["id"]
        sid_builtin = client.post(
            "/v1/sessions",
            json={"title": "builtin", "workspace_id": wid},
        ).json()["id"]
        assert client.post(
            f"/v1/sessions/{sid_blueprint}/agent-blueprint",
            json={"blueprint_id": "remote-data"},
        ).status_code == 200
        agents_blueprint = client.get(
            "/v1/agents",
            params={"session_id": sid_blueprint},
        ).json()["agents"]
        agents_builtin = client.get(
            "/v1/agents",
            params={"session_id": sid_builtin},
        ).json()["agents"]
        assistant = complete_turn(client, sid_blueprint, "prove runtime")

    assert [row["id"] for row in agents_blueprint] == ["data"]
    assert any(row["id"] == "analysis" for row in agents_builtin)
    assert calls == [
        {
            "agent_id": "data",
            "title": "Remote Data Orchestrator",
            "system_prompt": "REMOTE BLUEPRINT ORCHESTRATOR MARKER.",
            "question": "prove runtime",
            "session_id": sid_blueprint,
        }
    ]
    assert assistant["metadata"]["agent_runtime"]["agent_id"] == "data"
    assert assistant["metadata"]["agent_runtime"]["source"] == "expert_pack"
    assert assistant["metadata"]["agent_runtime"]["pack"]["id"] == "remote-data"


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
