from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from clio_agent.gact.agent_blueprints import (
    discover_agent_blueprints,
    load_agent_blueprint_path,
    load_agent_blueprints,
    validate_agent_blueprint_path,
)
from clio_agent.gact.app import (
    _build_prompt_user_agent_module,
    _builtin_agents,
    _dynamic_agent_runtime_provenance,
    _dynamic_agent_tools,
    _gact_app_context,
    _resolve_runtime_dynamic_agent,
    _runtime_dynamic_agent_children_context,
    build_app,
)
from clio_agent.gact.types import AgentDef
from clio_agent.runtime.hooks import install_global_registry
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


def _write_provider_profile_blueprint(root: Path) -> None:
    (root / "experts").mkdir(parents=True)
    (root / "prompts").mkdir()
    root.joinpath("AGENT.md").write_text(
        """---
id: provider-profile-agent
version: 0.1.0
title: Provider Profile Agent
root_expert: root
blueprint:
  format: agent-blueprint-v1
---
Provider/profile provenance test agent.
""",
        encoding="utf-8",
    )
    root.joinpath("experts", "root.md").write_text(
        """---
id: root
title: Root Expert
tier: 1
prompt_id: profile.root
prompt_profile: heavy
---
Inline root prompt should be replaced.
""",
        encoding="utf-8",
    )
    root.joinpath("experts", "analysis.md").write_text(
        """---
id: analysis
title: Analysis Expert
parent_id: root
tier: 2
prompt_id: profile.analysis
prompt_profile: light
---
Inline analysis prompt should be replaced.
""",
        encoding="utf-8",
    )
    root.joinpath("prompts", "profile.root.md").write_text(
        """---
id: profile.root
profile: heavy
provider: openai
model: gpt-5.1
---
Root prompt from blueprint profile.
""",
        encoding="utf-8",
    )
    root.joinpath("prompts", "profile.analysis.md").write_text(
        """---
id: profile.analysis
profile: light
provider: anthropic
model: claude-sonnet-4-20250514
---
Analysis prompt from blueprint profile.
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


def test_builtin_agents_are_loaded_from_packaged_blueprint() -> None:
    agents = {row.id: row for row in _builtin_agents()}

    assert {"main", "data", "analysis", "visualization", "ndp_catalog"} <= set(agents)
    assert agents["main"].metadata["source_blueprint"] == "builtin"
    assert agents["main"].metadata["definition_path"].endswith(
        "agent_blueprints/builtin/data-exploration/experts/main.md"
    )
    assert agents["main"].system_prompt.startswith("You are CLIO's agent planner.")
    assert agents["data"].metadata["definition_path"].endswith(
        "agent_blueprints/builtin/data-exploration/experts/data.md"
    )
    assert agents["data"].system_prompt.startswith("You are the CLIO Data Expert")


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
    assert any("compatibility mode" in warning for warning in body["validation_warnings"])


def test_agent_blueprint_v1_contract_rejects_missing_required_manifest_fields(
    tmp_path: Path,
) -> None:
    root = tmp_path / "broken"
    (root / "experts").mkdir(parents=True)
    root.joinpath("AGENT.md").write_text(
        """---
id: broken
blueprint:
  format: agent-blueprint-v1
---
Broken v1 agent.
""",
        encoding="utf-8",
    )
    root.joinpath("experts", "root.md").write_text(
        """---
id: root
tier: 1
---
Coordinate work.
""",
        encoding="utf-8",
    )

    body = validate_agent_blueprint_path(root)

    assert body["enabled"] is False
    errors = "\n".join(body["validation_errors"])
    assert "missing required blueprint field: version" in errors
    assert "missing required blueprint field: title" in errors
    assert "missing required blueprint field: root_expert" in errors
    assert "root: missing required expert field: title" in errors


def test_agent_blueprint_v1_contract_reports_unknown_fields_and_skill_gap(
    tmp_path: Path,
) -> None:
    root = tmp_path / "agent"
    (root / "experts").mkdir(parents=True)
    root.joinpath("AGENT.md").write_text(
        """---
id: agent
version: 1.0.0
title: Contract Agent
root_expert: root
blueprint:
  format: agent-blueprint-v1
surprise: ignored
---
Contract agent.
""",
        encoding="utf-8",
    )
    root.joinpath("experts", "root.md").write_text(
        """---
id: root
title: Root
description: Root expert.
tier: 1
skills:
  - pack.local.skill
unexpected_field: ignored
---
Coordinate work.
""",
        encoding="utf-8",
    )

    body = validate_agent_blueprint_path(root)

    assert body["enabled"] is True
    warnings = "\n".join(body["validation_warnings"])
    assert "unknown blueprint field ignored: surprise" in warnings
    assert "root: unknown expert field ignored: unexpected_field" in warnings
    assert "root: skills are resolved at runtime" in warnings
    rows = {row["id"]: row for row in body["agents"]}
    assert "validation_warnings" in rows["root"]["metadata"]


def test_agent_blueprint_includes_pack_local_expert_subtree(tmp_path: Path) -> None:
    root = tmp_path / "seismic"
    (root / "experts").mkdir(parents=True)
    (root / "modules" / "ndp-collector" / "experts").mkdir(parents=True)
    root.joinpath("AGENT.md").write_text(
        """---
id: seismic
version: 0.1.0
title: Seismic Agent
root_expert: main
blueprint:
  format: agent-blueprint-v1
includes:
  - modules/ndp-collector/experts
---
Seismic agent.
""",
        encoding="utf-8",
    )
    root.joinpath("experts", "main.md").write_text(
        """---
id: main
title: Main
description: Main expert.
tier: 1
---
Coordinate seismic work.
""",
        encoding="utf-8",
    )
    root.joinpath("experts", "data.md").write_text(
        """---
id: data
title: Data
description: Data expert.
tier: 2
parent_id: main
---
Own data access.
""",
        encoding="utf-8",
    )
    root.joinpath("modules", "ndp-collector", "experts", "ndp_catalog.md").write_text(
        """---
id: ndp_catalog
title: NDP Catalog
description: Catalog child expert.
tier: 3
parent_id: data
---
Search NDP and return compact catalog evidence.
""",
        encoding="utf-8",
    )

    body = validate_agent_blueprint_path(root)

    assert body["enabled"] is True
    rows = {row["id"]: row for row in body["agents"]}
    assert set(rows) == {"main", "data", "ndp_catalog"}
    assert rows["ndp_catalog"]["parent_id"] == "data"
    assert rows["ndp_catalog"]["metadata"]["agent_blueprint_include"] == "modules/ndp-collector/experts"
    assert rows["ndp_catalog"]["metadata"]["agent_blueprint_expert_source"] == "include"

    loaded = {row.id: row for row in load_agent_blueprint_path(root)}
    assert loaded["ndp_catalog"].metadata["agent_blueprint_include"] == "modules/ndp-collector/experts"


def test_agent_blueprint_include_validation_rejects_missing_or_empty_path(
    tmp_path: Path,
) -> None:
    root = tmp_path / "broken"
    (root / "experts").mkdir(parents=True)
    (root / "modules" / "empty").mkdir(parents=True)
    root.joinpath("AGENT.md").write_text(
        """---
id: broken
version: 0.1.0
title: Broken Agent
root_expert: main
blueprint:
  format: agent-blueprint-v1
includes:
  - modules/missing
  - modules/empty
---
Broken agent.
""",
        encoding="utf-8",
    )
    root.joinpath("experts", "main.md").write_text(
        """---
id: main
title: Main
description: Main expert.
tier: 1
---
Coordinate work.
""",
        encoding="utf-8",
    )

    body = validate_agent_blueprint_path(root)

    assert body["enabled"] is False
    errors = "\n".join(body["validation_errors"])
    assert "include path not found: modules/missing" in errors
    assert "include path contains no expert markdown files: modules/empty" in errors


def test_agent_blueprint_include_validation_rejects_path_escape(tmp_path: Path) -> None:
    root = tmp_path / "broken"
    (root / "experts").mkdir(parents=True)
    root.joinpath("AGENT.md").write_text(
        """---
id: broken
version: 0.1.0
title: Broken Agent
root_expert: main
blueprint:
  format: agent-blueprint-v1
includes:
  - ../outside
---
Broken agent.
""",
        encoding="utf-8",
    )
    root.joinpath("experts", "main.md").write_text(
        """---
id: main
title: Main
description: Main expert.
tier: 1
---
Coordinate work.
""",
        encoding="utf-8",
    )

    body = validate_agent_blueprint_path(root)

    assert body["enabled"] is False
    assert "include path must be pack-local: ../outside" in "\n".join(body["validation_errors"])


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


def test_agent_blueprint_root_runtime_context_lists_declared_children(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    blueprint = workspace / ".clio" / "agent-blueprints" / "genomics"
    _write_blueprint(blueprint)
    blueprint.joinpath("experts", "root.md").write_text(
        """---
id: root
title: Genomics Root
tier: 1
prompt_id: clio.main.planner
---
Coordinate genomics work.
""",
        encoding="utf-8",
    )

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
        assert client.post(
            f"/v1/sessions/{sid}/agent-blueprint",
            json={"blueprint_id": "genomics"},
        ).status_code == 200
        root = next(
            AgentDef(**row)
            for row in client.get("/v1/agents", params={"session_id": sid}).json()["agents"]
            if row["id"] == "root"
        )

    context = _runtime_dynamic_agent_children_context(app, root, session_id=sid)

    assert "Declared child experts" in context
    assert "variant: Variant Expert" in context
    assert "memory_search_sessions" in context
    assert "expert_handoffs JSON array" in context
    assert "{{" not in root.system_prompt
    assert "- variant: Variant Expert" in root.system_prompt


def test_agent_blueprint_declared_pack_skill_loads_into_runtime_prompt(
    tmp_path: Path,
) -> None:
    from clio_agent.config import LMProviderConfig

    workspace = tmp_path / "workspace"
    blueprint = workspace / ".clio" / "agent-blueprints" / "genomics"
    _write_blueprint(blueprint)
    blueprint.joinpath("experts", "root.md").write_text(
        """---
id: root
title: Genomics Root
tier: 1
skills:
  - variant_pathogenicity_triage
---
Coordinate genomics work.
""",
        encoding="utf-8",
    )
    skill_dir = blueprint / "skills" / "variant_pathogenicity_triage"
    skill_dir.mkdir(parents=True)
    skill_dir.joinpath("SKILL.md").write_text(
        """---
name: variant_pathogenicity_triage
title: Variant Pathogenicity Triage
---
Apply ACMG-style evidence buckets before making any variant interpretation.
""",
        encoding="utf-8",
    )
    base_agent = SimpleNamespace(
        _provider_config=LMProviderConfig(
            provider="openai",
            model="gpt-test",
            api_key="test",
        )
    )
    app = build_app(sessions_path=tmp_path / "sessions.json", agent=base_agent)

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
        assert client.post(
            f"/v1/sessions/{sid}/agent-blueprint",
            json={"blueprint_id": "genomics"},
        ).status_code == 200

    resolved = _resolve_runtime_dynamic_agent(app, "root", session_id=sid, workspace_id=wid)

    assert resolved is not None
    skill_resolution = resolved.metadata["skill_resolution"]
    assert skill_resolution["missing"] == []
    assert skill_resolution["resolved"][0]["scope"] == "pack"
    assert skill_resolution["resolved"][0]["id"] == "variant_pathogenicity_triage"

    module = _build_prompt_user_agent_module(base_agent, resolved)

    assert "Expert-declared skills loaded for this turn" in module.system_prompt
    assert "Apply ACMG-style evidence buckets" in module.system_prompt
    provenance = _dynamic_agent_runtime_provenance(
        app,
        resolved,
        execution_mode="prompt_agent",
    )
    assert provenance["skill_resolution"]["resolved"][0]["scope"] == "pack"
    assert provenance["resolved_skills"][0]["id"] == "variant_pathogenicity_triage"


def test_agent_blueprint_missing_declared_skill_is_runtime_diagnostic(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    blueprint = workspace / ".clio" / "agent-blueprints" / "genomics"
    _write_blueprint(blueprint)
    blueprint.joinpath("experts", "root.md").write_text(
        """---
id: root
title: Genomics Root
tier: 1
skills:
  - missing_domain_skill
---
Coordinate genomics work.
""",
        encoding="utf-8",
    )
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
        assert client.post(
            f"/v1/sessions/{sid}/agent-blueprint",
            json={"blueprint_id": "genomics"},
        ).status_code == 200

    resolved = _resolve_runtime_dynamic_agent(app, "root", session_id=sid, workspace_id=wid)

    assert resolved is not None
    assert resolved.metadata["skill_resolution"]["resolved"] == []
    assert resolved.metadata["skill_resolution"]["missing"] == [
        {"id": "missing_domain_skill", "status": "missing"}
    ]
    assert "declared skill not found at runtime: missing_domain_skill" in resolved.metadata[
        "validation_warnings"
    ]


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


def test_session_agent_overlay_rejects_invalid_contracts(tmp_path: Path) -> None:
    blueprint = tmp_path / "genomics"
    _write_blueprint(blueprint)

    app = build_app(sessions_path=tmp_path / "sessions.json", agent=SimpleNamespace())
    with TestClient(app) as client:
        sid = client.post("/v1/sessions", json={"title": "A"}).json()["id"]
        assert client.post(
            f"/v1/sessions/{sid}/agent-blueprint",
            json={"path": str(blueprint)},
        ).status_code == 200

        broken_parent = client.put(
            f"/v1/sessions/{sid}/agent-overlay",
            json={"agents": {"variant": {"parent_id": "missing-parent"}}},
        )
        bad_tool = client.put(
            f"/v1/sessions/{sid}/agent-overlay",
            json={"agents": {"variant": {"tools": ["definitely_missing_tool"]}}},
        )
        bad_provider = client.put(
            f"/v1/sessions/{sid}/agent-overlay",
            json={"agents": {"variant": {"default_provider": "definitely_missing_provider"}}},
        )
        bad_prompt = client.put(
            f"/v1/sessions/{sid}/agent-overlay",
            json={"agents": {"variant": {"prompt_id": "definitely.missing.prompt"}}},
        )
        unknown_agent = client.put(
            f"/v1/sessions/{sid}/agent-overlay",
            json={"agents": {"missing-agent": {"title": "Nope"}}},
        )
        overlay_state = client.get(f"/v1/sessions/{sid}/agent-overlay").json()

    assert broken_parent.status_code == 422
    assert "parent_id not found" in broken_parent.text
    assert bad_tool.status_code == 422
    assert "unknown tool" in bad_tool.text
    assert bad_provider.status_code == 422
    assert "provider not found" in bad_provider.text
    assert bad_prompt.status_code == 422
    assert "prompt not found" in bad_prompt.text
    assert unknown_agent.status_code == 422
    assert "unknown expert" in unknown_agent.text
    assert overlay_state["agent_overlay"] == {}
    assert overlay_state["validation"]["enabled"] is True


def test_session_agent_overlay_can_export_workspace_blueprint(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    source = tmp_path / "source" / "genomics"
    _write_blueprint(source)

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
            json={"title": "A", "workspace_id": wid},
        ).json()["id"]
        assert client.post(
            f"/v1/sessions/{sid}/agent-blueprint",
            json={"path": str(source)},
        ).status_code == 200
        saved = client.put(
            f"/v1/sessions/{sid}/agent-overlay",
            json={
                "agents": {
                    "variant": {
                        "title": "Session A Variant Expert",
                        "system_prompt": "Review this workspace's variant evidence.",
                    }
                }
            },
        )
        assert saved.status_code == 200, saved.text

        exported = client.post(
            f"/v1/sessions/{sid}/agent-overlay/export",
            json={
                "blueprint_id": "genomics-session-a",
                "title": "Genomics Session A",
                "workspace_id": wid,
            },
        )
        listed = client.get("/v1/agent-blueprints", params={"workspace_id": wid}).json()

    assert exported.status_code == 201, exported.text
    exported_root = workspace / ".clio" / "agent-blueprints" / "genomics-session-a"
    assert exported_root.joinpath("AGENT.md").exists()
    assert "Session A Variant Expert" in exported_root.joinpath("experts", "variant.md").read_text()
    assert "Variant Expert" in source.joinpath("experts", "variant.md").read_text()
    assert {row["id"] for row in exported.json()["agents"]} == {"root", "variant"}
    assert "genomics-session-a" in {row["id"] for row in listed["agent_blueprints"]}


def test_session_agent_overlay_prompt_provenance_reaches_prompts_and_turn_metadata(
    tmp_path: Path,
    monkeypatch,
) -> None:
    blueprint = tmp_path / "remote-data"
    _write_data_root_blueprint(blueprint)
    calls: list[dict[str, str]] = []

    async def no_stream(*args, **kwargs):
        return None

    def fake_prompt_runner(base_agent, agent_def, question, session_id, cancel_requested=None):
        del base_agent, cancel_requested
        calls.append(
            {
                "agent_id": agent_def.id,
                "system_prompt": agent_def.system_prompt,
                "model": agent_def.default_model,
                "question": question,
                "session_id": session_id,
            }
        )
        return SimpleNamespace(
            answer="overlay provenance ok",
            selected_expert=agent_def.id,
            routing_rationale="session overlay",
            route_source="agent_blueprint",
            error_info=None,
        )

    monkeypatch.setattr("clio_agent.gact.app._try_streamed_forward", no_stream)
    monkeypatch.setattr("clio_agent.gact.app._run_prompt_user_agent", fake_prompt_runner)

    app = build_app(sessions_path=tmp_path / "sessions.json", agent=SimpleNamespace())
    with TestClient(app) as client:
        sid = client.post("/v1/sessions", json={"title": "overlay runtime"}).json()["id"]
        assert client.post(
            f"/v1/sessions/{sid}/agent-blueprint",
            json={"path": str(blueprint)},
        ).status_code == 200
        saved = client.put(
            f"/v1/sessions/{sid}/agent-overlay",
            json={
                "agents": {
                    "data": {
                        "system_prompt": "SESSION OVERLAY PROMPT.",
                        "default_model": "gpt-5-mini",
                    }
                }
            },
        )
        assert saved.status_code == 200, saved.text
        prompts = client.get("/v1/prompts", params={"session_id": sid}).json()
        assistant = complete_turn(client, sid, "prove overlay provenance")

    overlay_sources = prompts["agent_overlay"]["agents"]
    assert overlay_sources == [
        {
            "agent_id": "data",
            "fields": ["default_model", "system_prompt"],
            "has_system_prompt": True,
            "prompt_id": "",
            "prompt_profile": "",
            "default_provider": "",
            "default_model": "gpt-5-mini",
            "source": "session_agent_overlay",
            "session_id": sid,
        }
    ]
    assert calls == [
        {
            "agent_id": "data",
            "system_prompt": "SESSION OVERLAY PROMPT.",
            "model": "gpt-5-mini",
            "question": "prove overlay provenance",
            "session_id": sid,
        }
    ]
    runtime = assistant["metadata"]["agent_runtime"]
    assert runtime["agent_blueprint"]["id"] == "remote-data"
    assert runtime["agent_overlay"]["status"] == "applied"
    assert runtime["agent_overlay"]["fields"] == ["default_model", "system_prompt"]
    assert runtime["prompt"]["source"] == "session_agent_overlay"


def test_agent_blueprint_prompt_profile_provider_runtime_provenance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    blueprint = tmp_path / "provider-profile-agent"
    _write_provider_profile_blueprint(blueprint)
    seen: list[dict[str, Any]] = []

    async def no_stream(*args: Any, **kwargs: Any) -> None:
        return None

    def fake_prompt_runner(
        base_agent: Any,
        agent_def: Any,
        question: str,
        session_id: str,
        cancel_requested: Any | None = None,
    ) -> Any:
        del base_agent, cancel_requested
        seen.append(
            {
                "agent_id": agent_def.id,
                "prompt": agent_def.system_prompt,
                "provider": agent_def.default_provider,
                "model": agent_def.default_model,
                "question": question,
                "session_id": session_id,
            }
        )
        handoffs = (
            json.dumps(
                [
                    {
                        "delegate_to": "analysis",
                        "question": "Use the analysis profile.",
                    }
                ]
            )
            if agent_def.id == "root"
            else "[]"
        )
        return SimpleNamespace(
            answer=f"{agent_def.id} done",
            selected_expert=agent_def.id,
            expert_handoffs=handoffs,
            routing_rationale="profile provenance",
            route_source="agent_blueprint",
            error_info=None,
        )

    monkeypatch.setattr("clio_agent.gact.app._try_streamed_forward", no_stream)
    monkeypatch.setattr("clio_agent.gact.app._run_prompt_user_agent", fake_prompt_runner)

    app = build_app(sessions_path=tmp_path / "sessions.json", agent=SimpleNamespace())
    with TestClient(app) as client:
        sid = client.post("/v1/sessions", json={"title": "provider profile"}).json()["id"]
        activated = client.post(
            f"/v1/sessions/{sid}/agent-blueprint",
            json={"path": str(blueprint)},
        )
        assert activated.status_code == 200, activated.text
        assistant = complete_turn(client, sid, "prove profile provenance")

    root_call = next(row for row in seen if row["agent_id"] == "root")
    analysis_call = next(row for row in seen if row["agent_id"] == "analysis")
    assert root_call["prompt"] == "Root prompt from blueprint profile."
    assert root_call["provider"] == "openai"
    assert root_call["model"] == "gpt-5.1"
    assert analysis_call["prompt"] == "Analysis prompt from blueprint profile."
    assert analysis_call["provider"] == "anthropic"
    assert analysis_call["model"] == "claude-sonnet-4-20250514"

    root_runtime = assistant["metadata"]["agent_runtime"]
    assert root_runtime["prompt"]["id"] == "profile.root"
    assert root_runtime["prompt"]["profile"] == "heavy"
    assert root_runtime["prompt"]["resolution"]["scope"] == "session_agent_blueprint"
    assert root_runtime["model"] == {
        "provider_id": "openai",
        "model_id": "gpt-5.1",
        "provider_source": "prompt_resolution",
        "model_source": "prompt_resolution",
        "fallback_to_global": False,
    }

    handoffs = assistant["metadata"]["expert_handoffs"]
    completed = next(row for row in handoffs if row.get("stage") == "delegate.completed")
    assert completed["agent_id"] == "analysis"
    assert completed["prompt_resolution"]["id"] == "profile.analysis"
    assert completed["prompt_resolution"]["profile"] == "light"
    assert completed["prompt_resolution"]["scope"] == "session_agent_blueprint"
    assert completed["provider"] == {
        "provider_id": "anthropic",
        "model_id": "claude-sonnet-4-20250514",
        "provider_source": "prompt_resolution",
        "model_source": "prompt_resolution",
        "fallback_to_global": False,
    }
    assert completed["agent_runtime"]["prompt"]["profile"] == "light"
    assert completed["agent_runtime"]["model"]["provider_source"] == "prompt_resolution"


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
    provenance = assistant["metadata"]["runtime_provenance"]
    assert provenance["schema_version"] == "clio.runtime_provenance.v1"
    assert provenance["turn"]["user_message_id"].startswith("msg_user_")
    assert provenance["turn"]["assistant_message_id"] == assistant["id"]
    assert provenance["workspace"]["workspace_id"] == wid
    assert provenance["workspace"]["root_path"] == str(workspace)
    assert provenance["blueprint"]["id"] == "remote-data"
    assert provenance["agent"]["runtime"]["agent_id"] == "data"
    assert provenance["agent"]["expert"]["id"] == "data"
    assert provenance["agent"]["expert"]["tier"] == 1
    assert set(provenance["provider"]) >= {
        "provider_id",
        "model_id",
        "provider_source",
        "model_source",
        "fallback_to_global",
    }
    assert provenance["prompt"]["source"] == "agent_definition"
    assert provenance["tools"]["declared"] == []
    assert provenance["delegation"]["events"] == []
    assert provenance["memory"]["policy"]["default_scope"] == "session"


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
    assert any("disabled until explicitly enabled" in warning for warning in descriptor["validation_warnings"])


def test_agent_blueprint_mcp_descriptor_derives_uvx_install_command(
    tmp_path: Path,
) -> None:
    root = tmp_path / "marketplace" / "calc"
    _write_blueprint(root, blueprint_id="calc")
    (root / "tools").mkdir()
    root.joinpath("tools", "calculator.md").write_text(
        """---
id: calculator
name: Calculator MCP
transport: stdio
install:
  method: uvx
  package: clio-calculator-mcp
runtime:
  args:
    - serve
tools:
  - calculator_add
trust:
  policy: explicit
env_policy:
  secrets: none
verification:
  probe: list_tools
---
Calculator descriptor.
""",
        encoding="utf-8",
    )

    body = validate_agent_blueprint_path(root)

    descriptor = body["mcp_descriptors"][0]
    assert descriptor["validation_errors"] == []
    assert descriptor["command"] == "uvx"
    assert descriptor["args"] == ["clio-calculator-mcp", "serve"]
    assert descriptor["install"] == {"method": "uvx", "package": "clio-calculator-mcp"}
    assert descriptor["runtime"] == {"args": ["serve"]}
    assert descriptor["trust"]["policy"] == "explicit"
    assert descriptor["env_policy"] == {"secrets": "none"}
    assert descriptor["verification"] == {"probe": "list_tools"}


def test_agent_blueprint_mcp_descriptor_derives_pack_local_launch(
    tmp_path: Path,
) -> None:
    root = tmp_path / "marketplace" / "local-calc"
    _write_blueprint(root, blueprint_id="local-calc")
    (root / "tools").mkdir()
    (root / "mcp").mkdir()
    (root / "mcp" / "calculator_server.py").write_text(
        "print('calculator')\n",
        encoding="utf-8",
    )
    root.joinpath("tools", "calculator.md").write_text(
        """---
id: calculator
name: Local Calculator MCP
transport: stdio
install:
  method: pack-local
  path: mcp/calculator_server.py
tools:
  - calculator_add
---
Calculator descriptor.
""",
        encoding="utf-8",
    )

    body = validate_agent_blueprint_path(root)

    descriptor = body["mcp_descriptors"][0]
    assert descriptor["validation_errors"] == []
    assert descriptor["command"] == "python"
    assert descriptor["args"] == [str(root / "mcp" / "calculator_server.py")]


def test_agent_blueprint_mcp_descriptor_rejects_missing_pack_local_launch(
    tmp_path: Path,
) -> None:
    root = tmp_path / "marketplace" / "local-calc"
    _write_blueprint(root, blueprint_id="local-calc")
    (root / "tools").mkdir()
    root.joinpath("tools", "calculator.md").write_text(
        """---
id: calculator
name: Local Calculator MCP
transport: stdio
install:
  method: pack-local
  path: mcp/missing_server.py
tools:
  - calculator_add
---
Calculator descriptor.
""",
        encoding="utf-8",
    )

    body = validate_agent_blueprint_path(root)

    assert body["enabled"] is False
    errors = "\n".join(body["validation_errors"])
    assert "calculator: pack-local MCP launch path not found: mcp/missing_server.py" in errors
    descriptor = body["mcp_descriptors"][0]
    assert descriptor["command"] == "python"
    assert descriptor["args"] == [str(root / "mcp" / "missing_server.py")]


def test_agent_blueprint_mcp_descriptor_validates_transport_requirements(
    tmp_path: Path,
) -> None:
    root = tmp_path / "marketplace" / "earth"
    _write_blueprint(root, blueprint_id="earth")
    (root / "tools").mkdir()
    root.joinpath("tools", "stdio.md").write_text(
        """---
id: local
transport: stdio
---
Missing command.
""",
        encoding="utf-8",
    )
    root.joinpath("tools", "http.md").write_text(
        """---
id: remote
transport: streamable-http
---
Missing URL.
""",
        encoding="utf-8",
    )

    body = validate_agent_blueprint_path(root)

    errors = "\n".join(body["validation_errors"])
    assert "local: stdio MCP descriptors require command" in errors
    assert "remote: streamable-http MCP descriptors require url" in errors


def test_agent_blueprint_mcp_tool_references_require_enablement(tmp_path: Path) -> None:
    root = tmp_path / "marketplace" / "earth"
    _write_blueprint(root, blueprint_id="earth")
    root.joinpath("experts", "variant.md").write_text(
        """---
id: variant
title: Variant Expert
parent_id: root
tier: 2
tools:
  - earthscope_query
---
Use the external EarthScope catalog.
""",
        encoding="utf-8",
    )
    (root / "tools").mkdir()
    root.joinpath("tools", "earthscope.md").write_text(
        """---
id: earthscope
name: EarthScope MCP
transport: stdio
command: earthscope-mcp
args:
  - serve
tools:
  - earthscope_query
---
EarthScope descriptor.
""",
        encoding="utf-8",
    )

    body = validate_agent_blueprint_path(root)
    rows = {row["id"]: row for row in body["agents"]}

    assert body["enabled"] is False
    assert "MCP tool requires explicit enablement" in "\n".join(body["validation_errors"])
    assert rows["variant"]["enabled"] is False
    assert rows["variant"]["metadata"]["tool_diagnostics"][0]["tool"] == "earthscope_query"
    assert body["mcp_descriptors"][0]["tools"][0]["status"] == "disabled"


def test_agent_blueprint_validation_reports_unknown_tools(tmp_path: Path) -> None:
    root = tmp_path / "marketplace" / "earth"
    _write_blueprint(root, blueprint_id="earth")
    root.joinpath("experts", "variant.md").write_text(
        """---
id: variant
title: Variant Expert
parent_id: root
tier: 2
tools:
  - missing_external_tool
---
Use an undeclared external tool.
""",
        encoding="utf-8",
    )

    body = validate_agent_blueprint_path(root)

    assert body["enabled"] is False
    assert "unknown tool reference: missing_external_tool" in "\n".join(
        body["validation_errors"]
    )


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
            json={"workspace_id": wid, "probe": False},
        )
        assert enabled.status_code == 200, enabled.text
        rows_after = client.get("/v1/mcp/servers", params={"workspace_id": wid}).json()["servers"]

    assert any(
        row.get("source") == "agent_blueprint" and row.get("enabled") is False
        for row in rows_before
    )
    assert enabled.json()["status"] == "enabled_pending_probe"
    assert any(row["id"] == "agent_blueprint_mcp_earth_earthscope" for row in rows_after)


def test_session_agent_blueprint_exposes_mcp_descriptor_scope(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    root = workspace / ".clio" / "agent-blueprints" / "calc"
    _write_blueprint(root, blueprint_id="calc")
    (root / "tools").mkdir()
    (root / "mcp").mkdir()
    (root / "mcp" / "calculator_server.py").write_text("print('ok')\n", encoding="utf-8")
    root.joinpath("tools", "calculator.md").write_text(
        """---
id: calculator
name: Calculator MCP
transport: stdio
install:
  method: pack-local
  path: mcp/calculator_server.py
runtime:
  args:
    - serve
tools:
  - calculator_add
trust:
  policy: explicit
---
Calculator descriptor.
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
        sid = client.post(
            "/v1/sessions",
            json={"title": "blueprint", "workspace_id": wid},
        ).json()["id"]
        activated = client.post(
            f"/v1/sessions/{sid}/agent-blueprint",
            json={"blueprint_id": "calc"},
        )
        body = client.get(f"/v1/sessions/{sid}/agent-blueprint").json()

    assert activated.status_code == 200, activated.text
    assert body["active_agent_blueprint_id"] == "calc"
    descriptor = body["mcp_descriptors"][0]
    assert descriptor["id"] == "calculator"
    assert descriptor["enabled"] is False
    assert descriptor["status"] == "disabled"
    assert descriptor["tools"][0]["name"] == "calculator_add"
    assert descriptor["tools"][0]["status"] == "disabled"
    assert descriptor["trust"] == {"policy": "explicit", "trusted": False}
    assert descriptor["install"] == {
        "method": "pack-local",
        "path": "mcp/calculator_server.py",
    }
    assert body["hook_descriptors"] == []


def test_agent_blueprint_mcp_enable_records_install_and_trust_metadata(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    root = workspace / ".clio" / "agent-blueprints" / "calc"
    _write_blueprint(root, blueprint_id="calc")
    (root / "tools").mkdir()
    root.joinpath("tools", "calculator.md").write_text(
        """---
id: calculator
name: Calculator MCP
transport: stdio
install:
  method: uvx
  package: clio-calculator-mcp
runtime:
  args:
    - serve
tools:
  - calculator_add
trust:
  policy: explicit
verification:
  probe: list_tools
---
Calculator descriptor.
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
        enabled = client.post(
            "/v1/agent-blueprints/calc/mcp/calculator/enable",
            json={"workspace_id": wid, "probe": False, "trust": True},
        )
        rows = client.get("/v1/mcp/servers", params={"workspace_id": wid}).json()["servers"]

    assert enabled.status_code == 200, enabled.text
    body = enabled.json()
    assert body["spec"]["command"] == "uvx"
    assert body["spec"]["args"] == ["clio-calculator-mcp", "serve"]
    assert body["trust"] == {"policy": "explicit", "trusted": True, "source": "request"}
    assert body["install"] == {"method": "uvx", "package": "clio-calculator-mcp"}
    assert body["runtime"] == {"args": ["serve"]}
    assert body["verification"] == {"probe": "list_tools"}
    listed = next(row for row in rows if row["id"] == "agent_blueprint_mcp_calc_calculator")
    assert listed["trust"]["trusted"] is True
    assert listed["install"]["method"] == "uvx"


def test_agent_blueprint_mcp_enable_requires_trust_when_config_disables_trust_always(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CLIO_GACT_MCP_TRUST_ALWAYS", "false")
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
        denied = client.post(
            "/v1/agent-blueprints/earth/mcp/earthscope/enable",
            json={"workspace_id": wid, "probe": False},
        )
        allowed = client.post(
            "/v1/agent-blueprints/earth/mcp/earthscope/enable",
            json={"workspace_id": wid, "probe": False, "trust": True},
        )

    assert denied.status_code == 403
    assert denied.json()["error"]["error"] == "mcp_untrusted"
    assert allowed.status_code == 200, allowed.text
    assert allowed.json()["trust"]["source"] == "request"


def test_agent_blueprint_packaged_hook_validation_reports_unknown_event(
    tmp_path: Path,
) -> None:
    root = tmp_path / "genomics"
    _write_blueprint(root)
    (root / "hooks").mkdir()
    root.joinpath("hooks", "not_a_hook_event.py").write_text(
        "def not_a_hook_event():\n    return None\n",
        encoding="utf-8",
    )

    body = validate_agent_blueprint_path(root)

    assert body["enabled"] is False
    assert body["hook_descriptors"][0]["id"] == "not_a_hook_event"
    assert body["hook_descriptors"][0]["validation_errors"] == [
        "unsupported hook event: not_a_hook_event"
    ]
    assert "not_a_hook_event: unsupported hook event: not_a_hook_event" in body[
        "validation_errors"
    ]


def test_agent_blueprint_packaged_hook_requires_enablement_and_blueprint_scope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeAgent:
        def forward(self, question: str, session_id: str = "default") -> Any:
            return SimpleNamespace(answer=f"default handled {question}", error_info=None)

    async def no_stream(*args: Any, **kwargs: Any) -> None:
        return None

    def fake_prompt_runner(
        base_agent: Any,
        agent_def: AgentDef,
        question: str,
        session_id: str,
        cancel_requested: Any = None,
    ) -> Any:
        del base_agent, agent_def, session_id, cancel_requested
        return SimpleNamespace(
            answer=f"blueprint handled {question}",
            selected_expert="root",
            routing_rationale="session blueprint",
            route_source="agent_blueprint",
            error_info=None,
        )

    monkeypatch.setattr("clio_agent.gact.app._try_streamed_forward", no_stream)
    monkeypatch.setattr("clio_agent.gact.app._run_prompt_user_agent", fake_prompt_runner)
    monkeypatch.setenv("CLIO_HOOKS_BACKEND", "local_python")
    monkeypatch.setenv("CLIO_HOOKS_DIR", str(tmp_path / "runtime-hooks"))
    monkeypatch.setenv("CLIO_GACT_HOOK_TRUST_ALWAYS", "false")
    monkeypatch.setenv("CLIO_SEMANTIC_TRACE_BACKEND", "file")
    monkeypatch.setenv("CLIO_SEMANTIC_TRACE_PATH", str(tmp_path / "semantic-traces"))
    install_global_registry(None)

    workspace = tmp_path / "workspace"
    root = workspace / ".clio" / "agent-blueprints" / "genomics"
    _write_blueprint(root)
    (root / "hooks").mkdir()
    root.joinpath("hooks", "pre_message.py").write_text(
        """
def pre_message(session_id, text):
    if "BLOCK_ME" in text:
        raise PermissionError("blocked by packaged blueprint hook")
""",
        encoding="utf-8",
    )

    try:
        app = build_app(sessions_path=tmp_path / "sessions.json", agent=FakeAgent())
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
            sid_default = client.post(
                "/v1/sessions",
                json={"title": "default", "workspace_id": wid},
            ).json()["id"]
            assert client.post(
                f"/v1/sessions/{sid_blueprint}/agent-blueprint",
                json={"blueprint_id": "genomics"},
            ).status_code == 200

            detail = client.get(
                "/v1/agent-blueprints/genomics",
                params={"workspace_id": wid},
            ).json()
            denied = client.post(
                "/v1/agent-blueprints/genomics/hooks/pre_message/enable",
                json={"workspace_id": wid},
            )
            before_enable = complete_turn(client, sid_blueprint, "BLOCK_ME before enable")
            enabled = client.post(
                "/v1/agent-blueprints/genomics/hooks/pre_message/enable",
                json={"workspace_id": wid, "trust": True},
            )
            capabilities = client.get("/v1/capabilities").json()["capabilities"]
            default_after_enable = complete_turn(client, sid_default, "BLOCK_ME default")
            blueprint_after_enable = complete_turn(client, sid_blueprint, "allowed after enable")
            blocked_ack = client.post(
                f"/v1/sessions/{sid_blueprint}/messages",
                json={"parts": [{"type": "text", "text": "BLOCK_ME after enable"}]},
            )
            assert blocked_ack.status_code == 200, blocked_ack.text
            blocked_user_message_id = blocked_ack.json()["message_id"]
            deadline = time.monotonic() + 5.0
            blocked_session: dict[str, Any] = {}
            while time.monotonic() < deadline:
                blocked_session = client.get(f"/v1/sessions/{sid_blueprint}").json()
                if blocked_session["status"] == "error":
                    break
                time.sleep(0.05)
            messages_after_block = client.get(
                f"/v1/sessions/{sid_blueprint}/messages"
            ).json()["messages"]
            trace_path = tmp_path / "semantic-traces" / f"{sid_blueprint}.semantic.jsonl"
            trace_rows = [
                json.loads(line)
                for line in trace_path.read_text(encoding="utf-8").splitlines()
            ]

    finally:
        install_global_registry(None)

    assert detail["hook_descriptors"][0]["id"] == "pre_message"
    assert detail["hook_descriptors"][0]["enabled"] is False
    assert denied.status_code == 403
    assert denied.json()["error"]["error"] == "hook_untrusted"
    assert before_enable["role"] == "assistant"
    assert before_enable.get("error_info") is None
    assert enabled.status_code == 200, enabled.text
    enabled_body = enabled.json()
    assert enabled_body["status"] == "enabled"
    assert enabled_body["trust"] == {
        "policy": "explicit",
        "trusted": True,
        "source": "request",
    }
    assert enabled_body["installed_path"].endswith("blueprints/genomics/pre_message.py")
    installed_path = Path(enabled_body["installed_path"])
    sidecar = json.loads(
        installed_path.with_name(f"{installed_path.name}.json").read_text(encoding="utf-8")
    )
    assert sidecar["source"] == "agent_blueprint"
    assert sidecar["agent_blueprint_id"] == "genomics"
    assert sidecar["definition_path"].endswith("hooks/pre_message.py")
    assert sidecar["checksum"] == enabled_body["checksum"]
    assert capabilities["x_clio_hook_events"]["pre_message"] == 1
    assert default_after_enable["role"] == "assistant"
    assert default_after_enable.get("error_info") is None
    assert blueprint_after_enable["role"] == "assistant"
    assert blueprint_after_enable.get("error_info") is None
    assert blocked_session["status"] == "error"
    blocked_index = next(
        index
        for index, row in enumerate(messages_after_block)
        if row["id"] == blocked_user_message_id
    )
    assert blocked_index == 0 or messages_after_block[blocked_index - 1]["role"] != "assistant"

    completed_dispatch = next(
        row
        for row in trace_rows
        if row["event_type"] == "hook.invocation.completed"
        and row["actor"].get("hook") == "pre_message"
        and row["payload"].get("handlers")
    )
    completed_handler = completed_dispatch["payload"]["handlers"][0]
    assert completed_handler["source"] == "agent_blueprint"
    assert completed_handler["agent_blueprint_id"] == "genomics"
    assert completed_handler["definition_path"].endswith("hooks/pre_message.py")
    assert completed_handler["installed_path"].endswith("blueprints/genomics/pre_message.py")
    assert completed_handler["checksum"] == enabled_body["checksum"]
    assert completed_handler["status"] == "completed"
    blocked_dispatch = next(
        row
        for row in trace_rows
        if row["event_type"] == "hook.pre_message.blocked"
        and row["payload"].get("handlers")
    )
    blocked_handler = blocked_dispatch["payload"]["handlers"][0]
    assert blocked_handler["source"] == "agent_blueprint"
    assert blocked_handler["status"] == "blocked"
    assert blocked_handler["error"] == "blocked by packaged blueprint hook"


def test_enabled_agent_blueprint_mcp_descriptor_exposes_declared_tools(tmp_path: Path) -> None:
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
tools:
  - earthscope_query
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
        enabled = client.post(
            "/v1/agent-blueprints/earth/mcp/earthscope/enable",
            json={"workspace_id": wid, "probe": False},
        )
        tools = client.get("/v1/tools").json()["tools"]
        detail = client.get("/v1/tools/earthscope_query").json()

    assert enabled.status_code == 200, enabled.text
    assert enabled.json()["tools_count"] == 1
    declared = next(row for row in tools if row["id"] == "earthscope_query")
    assert declared["source"] == "agent_blueprint_mcp_descriptor"
    assert declared["status"] == "enabled_pending_probe"
    assert declared["enabled"] is False
    assert detail["descriptor_id"] == "earthscope"


def test_enabled_agent_blueprint_mcp_descriptor_probes_and_calls_tool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FakeClient:
        called_tool = ""

        def __init__(self, transport: Any) -> None:
            self.transport = transport

        async def __aenter__(self) -> "FakeClient":
            return self

        async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
            return None

        async def list_tools(self) -> list[Any]:
            return [
                SimpleNamespace(
                    name="earthscope_query",
                    description="query EarthScope catalog",
                    inputSchema={"type": "object"},
                    outputSchema={"type": "object"},
                )
            ]

        async def call_tool(self, name: str, args: dict[str, Any]) -> Any:
            FakeClient.called_tool = name
            return SimpleNamespace(
                content=[SimpleNamespace(type="text", text=f"{name}:{args['q']}")],
                isError=False,
            )

    import fastmcp
    import fastmcp.client.transports as transports

    monkeypatch.setattr(fastmcp, "Client", FakeClient)
    monkeypatch.setattr(transports, "StdioTransport", lambda command, args: (command, args))

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
tools:
  - earthscope_query
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
        enabled = client.post(
            "/v1/agent-blueprints/earth/mcp/earthscope/enable",
            json={"workspace_id": wid},
        )
        call = client.post(
            "/v1/mcp/servers/agent_blueprint_mcp_earth_earthscope/call",
            json={"tool": "earthscope_query", "args": {"q": "ANMO"}},
        )
        tools = client.get("/v1/tools").json()["tools"]

    assert enabled.status_code == 200, enabled.text
    body = enabled.json()
    assert body["status"] == "ready"
    assert body["tools"][0]["enabled"] is True
    assert body["tools"][0]["input_schema"] == {"type": "object"}
    assert call.status_code == 200, call.text
    assert FakeClient.called_tool == "earthscope_query"
    assert call.json()["content"] == [
        {"type": "text", "text": "earthscope_query:ANMO"}
    ]
    declared = next(row for row in tools if row["id"] == "earthscope_query")
    assert declared["enabled"] is True
    assert declared["status"] == "ready"


def test_enabled_agent_blueprint_mcp_tool_reenables_session_expert(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeClient:
        def __init__(self, transport: Any) -> None:
            self.transport = transport

        async def __aenter__(self) -> "FakeClient":
            return self

        async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
            return None

        async def list_tools(self) -> list[Any]:
            return [
                SimpleNamespace(
                    name="earthscope_query",
                    description="query EarthScope catalog",
                    inputSchema={"type": "object"},
                    outputSchema={"type": "object"},
                )
            ]

    import fastmcp
    import fastmcp.client.transports as transports

    monkeypatch.setattr(fastmcp, "Client", FakeClient)
    monkeypatch.setattr(transports, "StdioTransport", lambda command, args: (command, args))

    workspace = tmp_path / "workspace"
    root = workspace / ".clio" / "agent-blueprints" / "earth"
    _write_blueprint(root, blueprint_id="earth")
    root.joinpath("experts", "variant.md").write_text(
        """---
id: variant
title: Variant Expert
parent_id: root
tier: 2
tools:
  - earthscope_query
---
Use the external EarthScope catalog.
""",
        encoding="utf-8",
    )
    (root / "tools").mkdir()
    root.joinpath("tools", "earthscope.md").write_text(
        """---
id: earthscope
name: EarthScope MCP
transport: stdio
command: earthscope-mcp
args:
  - serve
tools:
  - earthscope_query
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
        sid = client.post(
            "/v1/sessions",
            json={"title": "earth", "workspace_id": wid},
        ).json()["id"]
        assert client.post(
            f"/v1/sessions/{sid}/agent-blueprint",
            json={"blueprint_id": "earth"},
        ).status_code == 200
        before = {
            row["id"]: row
            for row in client.get("/v1/agents", params={"session_id": sid}).json()["agents"]
        }
        enabled = client.post(
            "/v1/agent-blueprints/earth/mcp/earthscope/enable",
            json={"workspace_id": wid},
        )
        after = {
            row["id"]: row
            for row in client.get("/v1/agents", params={"session_id": sid}).json()["agents"]
        }

    assert before["variant"]["enabled"] is False
    assert "MCP tool requires explicit enablement" in "\n".join(
        before["variant"]["validation_errors"]
    )
    assert enabled.status_code == 200, enabled.text
    assert after["variant"]["enabled"] is True
    assert after["variant"]["validation_errors"] == []
    assert "tool_diagnostics" not in after["variant"]["metadata"]


def test_dynamic_agent_tools_include_enabled_agent_blueprint_mcp_tool(tmp_path: Path) -> None:
    app = build_app(sessions_path=tmp_path / "sessions.json")
    app.state.external_mcp_servers = {
        "agent_blueprint_mcp_earth_earthscope": {
            "id": "agent_blueprint_mcp_earth_earthscope",
            "name": "EarthScope MCP",
            "status": "ready",
            "spec": {
                "transport": "stdio",
                "command": "earthscope-mcp",
                "args": ["serve"],
            },
            "tools": [
                {
                    "id": "earthscope_query",
                    "name": "earthscope_query",
                    "description": "query EarthScope catalog",
                    "status": "ready",
                    "enabled": True,
                    "input_schema": {
                        "type": "object",
                        "properties": {"q": {"type": "string"}},
                    },
                }
            ],
        }
    }
    base_agent = SimpleNamespace(
        tool_executor=SimpleNamespace(to_dspy_tools=lambda: []),
    )
    agent_def = AgentDef(
        id="variant",
        source="expert_pack",
        title="Variant",
        tools=["earthscope_query"],
    )

    with _gact_app_context(app):
        tools = _dynamic_agent_tools(base_agent, agent_def)

    assert [tool.name for tool in tools] == ["earthscope_query"]
