from __future__ import annotations

from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from clio_agent.gact.app import build_app
from clio_agent.gact.expert_packs import (
    discover_expert_packs,
    load_expert_packs,
    parse_expert_file,
)
from clio_agent.gact.types import AgentDef


@pytest.fixture()
def isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    from tests.conftest import _write_test_default_registry_blueprint

    home = tmp_path / "home"
    home.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("HOME", str(home))
    xdg_root = home / ".config"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg_root))
    _write_test_default_registry_blueprint(xdg_root)
    monkeypatch.chdir(workspace)
    return workspace


def test_parse_expert_file_with_hierarchy_prompt_and_model(tmp_path: Path) -> None:
    path = tmp_path / "ndp.md"
    path.write_text(
        """---
id: ndp_catalog
title: NDP Catalog Expert
description: Dataset discovery specialist
parent_id: data
tier: 3
specialization: catalog
keywords:
- ndp
- earthscope
tools:
- ndp.search
- ndp.stage
prompt_id: clio.expert.ndp_catalog
prompt_profile: heavy
provider: openai
model: gpt-5.1
param_temperature: 0.2
---
Use bounded dataset discovery.
""",
        encoding="utf-8",
    )

    row = parse_expert_file(path, scope="workspace")

    assert row.id == "ndp_catalog"
    assert row.source == "expert_pack"
    assert row.parent_id == "data"
    assert row.tier == 3
    assert row.system_prompt == "Use bounded dataset discovery."
    assert row.prompt_id == "clio.expert.ndp_catalog"
    assert row.prompt_profile == "heavy"
    assert row.default_provider == "openai"
    assert row.default_model == "gpt-5.1"
    assert row.tools == ["ndp.search", "ndp.stage"]
    assert row.keywords == ["ndp", "earthscope"]
    assert row.parameters["temperature"] == 0.2
    assert row.enabled is True
    assert row.validation_errors == []


def test_parse_expert_file_with_skills_commands_and_capability_refs(tmp_path: Path) -> None:
    path = tmp_path / "market.md"
    path.write_text(
        """---
id: market_writer
title: Market Writer
parent_id: main
tier: 2
tools: [web.search, fs.read]
skills: [market_research]
commands: [summarize-market]
capability_refs: command:brief-market, skill:copy_edit
prompt_id: clio.expert.market_writer
provider: openai
model: gpt-5.1
---
Write market reports.
""",
        encoding="utf-8",
    )

    row = parse_expert_file(path, scope="workspace")

    assert row.tools == ["web.search", "fs.read"]
    assert row.skills == ["market_research"]
    assert row.commands == ["summarize-market"]
    refs = {(ref.kind, ref.id) for ref in row.capability_refs}
    assert ("command", "brief-market") in refs
    assert ("skill", "copy_edit") in refs
    assert row.default_provider == "openai"
    assert row.default_model == "gpt-5.1"


def test_invalid_expert_file_is_disabled_with_errors(tmp_path: Path) -> None:
    path = tmp_path / "broken.md"
    path.write_text(
        """---
tier: 3
---
""",
        encoding="utf-8",
    )

    row = parse_expert_file(path, scope="workspace")

    assert row.enabled is False
    assert row.id == "broken"
    assert "missing required frontmatter field: id" in row.validation_errors
    assert "tier > 1 experts must declare parent_id" in row.validation_errors
    assert "expert must provide a prompt body or prompt_id" in row.validation_errors


def test_prompt_id_expert_preserves_markdown_body(
    isolated_env: Path,
    tmp_path: Path,
) -> None:
    from clio_agent.gact.app import _resolve_runtime_dynamic_agent

    root = isolated_env / ".clio" / "experts"
    root.mkdir(parents=True)
    root.joinpath("waveform_main.md").write_text(
        """---
id: waveform_main
title: Waveform Main
parent_id: analysis
tier: 2
prompt_id: clio.main.planner
prompt_profile: heavy
---
UNIQUE_PACK_FALLBACK_POLICY: use IU.ANMO.00.BHZ when no better bounds are available.
""",
        encoding="utf-8",
    )

    app = build_app(sessions_path=tmp_path / "sessions.json", agent=object())
    resolved = _resolve_runtime_dynamic_agent(app, "waveform_main")

    assert resolved is not None
    assert "UNIQUE_PACK_FALLBACK_POLICY" in resolved.system_prompt
    assert "Agent-specific instructions from this definition" in resolved.system_prompt
    prompt_resolution = resolved.metadata["prompt_resolution"]
    assert prompt_resolution["id"] == "clio.main.planner"
    assert prompt_resolution["status"] == "resolved"
    assert prompt_resolution["composed_with_agent_body"] is True


def test_delegated_expert_prompt_appends_parent_evidence() -> None:
    from clio_agent.gact.app import _delegated_expert_prompt

    prompt = _delegated_expert_prompt(
        {
            "delegate_to": "variant_impact",
            "question": "Assess high-impact variants and verification steps.",
        },
        (
            "VCF evidence: file=/tmp/clio-benchmark-data/pathogen_sample_variants.vcf; "
            "effects=frameshift, stop_gained."
        ),
    )

    assert prompt.startswith("Assess high-impact variants")
    assert "Parent evidence available for this delegated task" in prompt
    assert "/tmp/clio-benchmark-data/pathogen_sample_variants.vcf" in prompt
    assert "stop_gained" in prompt


def test_manifest_pack_loads_nested_experts_with_pack_metadata(isolated_env: Path) -> None:
    pack = isolated_env / ".clio" / "expert-packs" / "data-semantics"
    (pack / "experts" / "data").mkdir(parents=True)
    pack.joinpath("clio-pack.yaml").write_text(
        """id: data-semantics
version: 0.1.0
title: Data Semantics
description: Data interpretation experts.
default_root_expert: data
defaults:
  prompt_profile: heavy
  provider: openai
  model: gpt-5.1
""",
        encoding="utf-8",
    )
    pack.joinpath("experts", "data.md").write_text(
        """---
id: data
title: Data Expert
parent_id: main
tier: 2
prompt_id: data.root
---
Coordinate data work.
""",
        encoding="utf-8",
    )
    pack.joinpath("experts", "data", "catalog.md").write_text(
        """---
id: data.catalog
title: Catalog Expert
parent_id: data
tier: 3
tools: [ndp.search]
skills: [catalog_reasoning]
commands: [summarize-dataset]
prompt_id: data.catalog
---
Search catalogs.
""",
        encoding="utf-8",
    )

    packs = {row.id: row for row in discover_expert_packs(cwd=isolated_env)}
    rows = {row.id: row for row in load_expert_packs(cwd=isolated_env)}

    assert packs["data-semantics"].version == "0.1.0"
    catalog = rows["data.catalog"]
    assert catalog.parent_id == "data"
    assert catalog.prompt_profile == "heavy"
    assert catalog.default_provider == "openai"
    assert catalog.default_model == "gpt-5.1"
    assert catalog.tools == ["ndp.search"]
    assert catalog.skills == ["catalog_reasoning"]
    assert catalog.commands == ["summarize-dataset"]
    assert catalog.metadata["pack_id"] == "data-semantics"
    assert catalog.metadata["pack_version"] == "0.1.0"
    assert catalog.metadata["pack_scope"] == "workspace"


def test_manifest_pack_allows_deeper_than_tier_three_hierarchy(isolated_env: Path) -> None:
    pack = isolated_env / ".clio" / "expert-packs" / "deep-science"
    (pack / "experts" / "biology" / "genomics").mkdir(parents=True)
    pack.joinpath("clio-pack.yaml").write_text(
        """id: deep-science
version: 0.1.0
title: Deep Science
""",
        encoding="utf-8",
    )
    pack.joinpath("experts", "biology.md").write_text(
        """---
id: biology
title: Biology
parent_id: main
tier: 2
---
Coordinate biology work.
""",
        encoding="utf-8",
    )
    pack.joinpath("experts", "biology", "genomics.md").write_text(
        """---
id: genomics
title: Genomics
parent_id: biology
tier: 3
---
Coordinate genomics work.
""",
        encoding="utf-8",
    )
    pack.joinpath("experts", "biology", "genomics", "clinvar.md").write_text(
        """---
id: clinvar_lookup
title: ClinVar Lookup
parent_id: genomics
tier: 4
tools: [clinvar.search]
---
Interpret ClinVar records.
""",
        encoding="utf-8",
    )

    rows = {row.id: row for row in load_expert_packs(cwd=isolated_env)}

    assert rows["clinvar_lookup"].tier == 4
    assert rows["clinvar_lookup"].parent_id == "genomics"
    assert rows["clinvar_lookup"].enabled is True
    assert rows["clinvar_lookup"].validation_errors == []


def test_load_expert_packs_workspace_overrides_global(
    isolated_env: Path,
    tmp_path: Path,
) -> None:
    global_root = tmp_path / "home" / ".config" / "clio-agent" / "experts"
    global_root.mkdir(parents=True)
    global_root.joinpath("reviewer.md").write_text(
        """---
id: reviewer
title: Global Reviewer
parent_id: main
---
global prompt
""",
        encoding="utf-8",
    )
    workspace_root = isolated_env / ".clio" / "experts"
    workspace_root.mkdir(parents=True)
    workspace_root.joinpath("reviewer.md").write_text(
        """---
id: reviewer
title: Workspace Reviewer
parent_id: main
---
workspace prompt
""",
        encoding="utf-8",
    )

    rows = {row.id: row for row in load_expert_packs(cwd=isolated_env)}

    assert rows["reviewer"].title == "Workspace Reviewer"
    assert rows["reviewer"].system_prompt == "workspace prompt"
    assert rows["reviewer"].metadata["expert_scope"] == "workspace"


def test_agents_catalog_includes_expert_pack_and_parent_metadata(
    isolated_env: Path,
    tmp_path: Path,
) -> None:
    root = isolated_env / ".clio" / "experts"
    root.mkdir(parents=True)
    root.joinpath("csv_quality.md").write_text(
        """---
id: csv_quality
title: CSV Quality Expert
description: Checks CSV quality
parent_id: analysis
tier: 3
keywords: csv, quality
tools: csv.inspect
prompt_id: clio.expert.csv_quality
profile: light
---
Check CSV schemas and quality.
""",
        encoding="utf-8",
    )

    client = TestClient(build_app(sessions_path=tmp_path / "sessions.json"))
    body = client.get("/v1/agents").json()
    rows = {row["id"]: row for row in body["agents"]}

    assert rows["analysis"]["parent_id"] == "main"
    assert rows["sac_format"]["parent_id"] == "analysis"
    expert = rows["csv_quality"]
    assert expert["source"] == "expert_pack"
    assert expert["parent_id"] == "analysis"
    assert expert["tier"] == 3
    assert expert["prompt_id"] == "clio.expert.csv_quality"
    assert expert["prompt_profile"] == "light"
    assert expert["enabled"] is True
    assert expert["metadata"]["expert_scope"] == "workspace"


def test_agents_catalog_allows_workspace_expert_to_override_builtin(
    isolated_env: Path,
    tmp_path: Path,
) -> None:
    pack = isolated_env / ".clio" / "expert-packs" / "data-override"
    (pack / "experts").mkdir(parents=True)
    pack.joinpath("clio-pack.yaml").write_text(
        """id: data-override
version: 0.1.0
title: Data Override
""",
        encoding="utf-8",
    )
    pack.joinpath("experts", "data.md").write_text(
        """---
id: data
title: Workspace Data Expert
parent_id: main
tier: 2
prompt_id: custom.data
---
Use the workspace data semantics.
""",
        encoding="utf-8",
    )

    client = TestClient(build_app(sessions_path=tmp_path / "sessions.json"))
    rows = {row["id"]: row for row in client.get("/v1/agents").json()["agents"]}

    assert rows["data"]["source"] == "expert_pack"
    assert rows["data"]["title"] == "Workspace Data Expert"
    assert rows["data"]["metadata"]["pack_id"] == "data-override"
    assert rows["data"]["metadata"]["override_chain"][0]["source"] == "expert_pack"


def test_expert_pack_apis_discover_validate_and_activate_per_session(
    isolated_env: Path,
    tmp_path: Path,
) -> None:
    packs_root = isolated_env / ".clio" / "expert-packs"
    for pack_id, title, expert_id in (
        ("data-semantics", "Data Semantics", "data_root"),
        ("wtfp-writer", "WTFP Writer", "writer"),
    ):
        pack = packs_root / pack_id
        (pack / "experts").mkdir(parents=True)
        pack.joinpath("clio-pack.yaml").write_text(
            f"""id: {pack_id}
version: 0.1.0
title: {title}
default_root_expert: {expert_id}
defaults:
  prompt_profile: light
""",
            encoding="utf-8",
        )
        pack.joinpath("experts", f"{expert_id}.md").write_text(
            f"""---
id: {expert_id}
title: {title} Root
parent_id: main
tier: 2
prompt_id: {pack_id}.root
---
Run the pack.
""",
            encoding="utf-8",
        )

    client = TestClient(build_app(sessions_path=tmp_path / "sessions.json"))
    packs = client.get("/v1/expert-packs").json()["expert_packs"]
    assert {row["id"] for row in packs} >= {"data-semantics", "wtfp-writer"}

    validate_body = client.post(
        "/v1/expert-packs/validate",
        json={"path": str(packs_root / "data-semantics")},
    ).json()
    assert validate_body["enabled"] is True
    assert validate_body["validation_errors"] == []

    sid_data = client.post("/v1/sessions", json={"title": "data"}).json()["id"]
    sid_writer = client.post("/v1/sessions", json={"title": "writer"}).json()["id"]
    data_pack = client.post(
        f"/v1/sessions/{sid_data}/expert-pack",
        json={"pack_id": "data-semantics"},
    ).json()
    writer_pack = client.post(
        f"/v1/sessions/{sid_writer}/expert-pack",
        json={"pack_id": "wtfp-writer"},
    ).json()

    assert data_pack["active_expert_pack_id"] == "data-semantics"
    assert writer_pack["active_expert_pack_id"] == "wtfp-writer"
    assert client.get(f"/v1/sessions/{sid_data}/expert-pack").json()[
        "active_expert_pack_id"
    ] == "data-semantics"

    data_agents = {
        row["id"] for row in client.get("/v1/agents", params={"session_id": sid_data}).json()["agents"]
    }
    writer_agents = {
        row["id"]
        for row in client.get("/v1/agents", params={"session_id": sid_writer}).json()["agents"]
    }
    assert "data_root" in data_agents
    assert "writer" not in data_agents
    assert "writer" in writer_agents
    assert "data_root" not in writer_agents


def test_session_pack_path_overrides_workspace_pack_only_for_that_session(
    isolated_env: Path,
    tmp_path: Path,
) -> None:
    workspace_pack = isolated_env / ".clio" / "expert-packs" / "workspace-data"
    (workspace_pack / "experts").mkdir(parents=True)
    workspace_pack.joinpath("clio-pack.yaml").write_text(
        """id: workspace-data
version: 0.1.0
title: Workspace Data
""",
        encoding="utf-8",
    )
    workspace_pack.joinpath("experts", "data.md").write_text(
        """---
id: data
title: Workspace Data Expert
parent_id: main
tier: 2
---
Workspace data behavior.
""",
        encoding="utf-8",
    )
    session_pack = tmp_path / "session-pack"
    (session_pack / "experts").mkdir(parents=True)
    session_pack.joinpath("clio-pack.yaml").write_text(
        """id: session-data
version: 0.2.0
title: Session Data
""",
        encoding="utf-8",
    )
    session_pack.joinpath("experts", "data.md").write_text(
        """---
id: data
title: Session Data Expert
parent_id: main
tier: 2
---
Session data behavior.
""",
        encoding="utf-8",
    )

    client = TestClient(build_app(sessions_path=tmp_path / "sessions.json"))
    sid_session = client.post("/v1/sessions", json={"title": "session override"}).json()["id"]
    sid_workspace = client.post("/v1/sessions", json={"title": "workspace default"}).json()["id"]

    activated = client.post(
        f"/v1/sessions/{sid_session}/expert-pack",
        json={"path": str(session_pack)},
    ).json()

    assert activated["active_expert_pack_id"] == "session-data"
    assert activated["active_expert_pack_path"] == str(session_pack)
    session_agents = {
        row["id"]: row
        for row in client.get("/v1/agents", params={"session_id": sid_session}).json()["agents"]
    }
    workspace_agents = {
        row["id"]: row
        for row in client.get("/v1/agents", params={"session_id": sid_workspace}).json()["agents"]
    }
    assert session_agents["data"]["title"] == "Session Data Expert"
    assert session_agents["data"]["metadata"]["pack_scope"] == "session"
    assert session_agents["data"]["metadata"]["override_chain"][-1]["scope"] == "session"
    assert workspace_agents["data"]["title"] == "Workspace Data Expert"
    assert workspace_agents["data"]["metadata"]["pack_scope"] == "workspace"


def test_workspace_pack_catalog_does_not_leak_between_workspaces(tmp_path: Path) -> None:
    home = tmp_path / "home"
    ws_a = tmp_path / "workspace-a"
    ws_b = tmp_path / "workspace-b"
    home.mkdir()
    ws_a.mkdir()
    ws_b.mkdir()
    pack_a = ws_a / ".clio" / "expert-packs" / "a-pack"
    pack_b = ws_b / ".clio" / "expert-packs" / "b-pack"
    for pack, pack_id, expert_id in (
        (pack_a, "a-pack", "a_expert"),
        (pack_b, "b-pack", "b_expert"),
    ):
        (pack / "experts").mkdir(parents=True)
        pack.joinpath("clio-pack.yaml").write_text(
            f"id: {pack_id}\nversion: 0.1.0\ntitle: {pack_id}\n",
            encoding="utf-8",
        )
        pack.joinpath("experts", f"{expert_id}.md").write_text(
            f"""---
id: {expert_id}
parent_id: main
tier: 2
---
Prompt.
""",
            encoding="utf-8",
        )

    client = TestClient(build_app(sessions_path=tmp_path / "sessions.json"))
    wid_a = client.post(
        "/v1/workspaces",
        json={"name": "A", "root_path": str(ws_a), "storage_root": str(ws_a / ".clio")},
    ).json()["id"]
    wid_b = client.post(
        "/v1/workspaces",
        json={"name": "B", "root_path": str(ws_b), "storage_root": str(ws_b / ".clio")},
    ).json()["id"]

    packs_a = {
        row["id"] for row in client.get("/v1/expert-packs", params={"workspace_id": wid_a}).json()["expert_packs"]
    }
    packs_b = {
        row["id"] for row in client.get("/v1/expert-packs", params={"workspace_id": wid_b}).json()["expert_packs"]
    }

    assert "a-pack" in packs_a
    assert "b-pack" not in packs_a
    assert "b-pack" in packs_b
    assert "a-pack" not in packs_b


def test_validate_expert_pack_surfaces_invalid_manifest_and_hierarchy(
    isolated_env: Path,
    tmp_path: Path,
) -> None:
    pack = isolated_env / ".clio" / "expert-packs" / "broken"
    (pack / "experts").mkdir(parents=True)
    pack.joinpath("clio-pack.yaml").write_text(
        """version: 0.1.0
default_root_expert: missing
""",
        encoding="utf-8",
    )
    pack.joinpath("experts", "orphan.md").write_text(
        """---
id: orphan
parent_id: absent
tier: 3
---
Prompt.
""",
        encoding="utf-8",
    )

    client = TestClient(build_app(sessions_path=tmp_path / "sessions.json"))
    body = client.post("/v1/expert-packs/validate", json={"path": str(pack)}).json()

    assert body["enabled"] is False
    assert "missing required manifest field: id" in body["validation_errors"]
    assert any("parent_id not found: absent" in error for error in body["validation_errors"])


def test_agents_catalog_surfaces_disabled_expert_with_missing_parent(
    isolated_env: Path,
    tmp_path: Path,
) -> None:
    root = isolated_env / ".clio" / "experts"
    root.mkdir(parents=True)
    root.joinpath("orphan.md").write_text(
        """---
id: orphan
title: Orphan Expert
parent_id: missing_parent
tier: 2
---
prompt
""",
        encoding="utf-8",
    )

    client = TestClient(build_app(sessions_path=tmp_path / "sessions.json"))
    rows = {row["id"]: row for row in client.get("/v1/agents").json()["agents"]}

    assert rows["orphan"]["enabled"] is False
    assert "parent_id not found: missing_parent" in rows["orphan"]["validation_errors"]


def test_capabilities_advertise_expert_packs(isolated_env: Path, tmp_path: Path) -> None:
    client = TestClient(build_app(sessions_path=tmp_path / "sessions.json"))

    caps = client.get("/v1/capabilities").json()["capabilities"]

    assert caps["x_clio_expert_packs"] is True


def test_expert_pack_agent_can_be_selected_and_executed(
    isolated_env: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from .conftest import complete_turn

    calls: list[tuple[str, str, str]] = []
    module = object()

    def fake_module(base_agent: Any, agent_def: Any) -> object:
        del base_agent
        assert agent_def.id == "csv_quality"
        assert agent_def.source == "expert_pack"
        assert agent_def.parent_id == "analysis"
        assert agent_def.prompt_profile == "light"
        return module

    async def fake_stream_unavailable(
        app: Any,
        enriched_text: str,
        sid: str,
        emit_chunk: Any,
        **kwargs: Any,
    ) -> None:
        del enriched_text, emit_chunk
        assert kwargs["agent_override"] is module
        from clio_agent.gact.app import _record_stream_fallback

        _record_stream_fallback(app, sid, "dynamic_prompt_stream_unavailable")
        return None

    def fake_prompt_agent(base_agent: Any, agent_def: Any, question: str, session_id: str) -> Any:
        del base_agent
        calls.append((agent_def.id, question, session_id))
        return type(
            "Pred",
            (),
            {
                "answer": "CSV_QUALITY_OK",
                "selected_expert": agent_def.id,
                "routing_rationale": "session selected expert-pack agent",
            },
        )()

    monkeypatch.setattr("clio_agent.gact.app._build_prompt_user_agent_module", fake_module)
    monkeypatch.setattr("clio_agent.gact.app._try_streamed_forward", fake_stream_unavailable)
    monkeypatch.setattr("clio_agent.gact.app._run_prompt_user_agent", fake_prompt_agent)

    root = isolated_env / ".clio" / "experts"
    root.mkdir(parents=True)
    root.joinpath("csv_quality.md").write_text(
        """---
id: csv_quality
title: CSV Quality Expert
parent_id: analysis
tier: 3
profile: light
---
Check CSV schemas and quality.
""",
        encoding="utf-8",
    )

    app = build_app(sessions_path=tmp_path / "sessions.json", agent=object())
    with TestClient(app) as client:
        sid = client.post(
            "/v1/sessions",
            json={"title": "expert run", "agent": {"id": "csv_quality"}},
        ).json()["id"]
        assistant = complete_turn(client, sid, "inspect data.csv")

    assert calls == [("csv_quality", "inspect data.csv", sid)]
    assert assistant["stop_reason"] == "end_turn"
    assert assistant["parts"][0]["selected_agent"] == "csv_quality"
    assert assistant["parts"][1]["text"] == "CSV_QUALITY_OK"
    assert assistant["metadata"]["stream_fallback"]["reason"] == "dynamic_prompt_stream_unavailable"


def test_expert_pack_agent_executes_delegated_child_expert(
    isolated_env: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from .conftest import complete_turn

    calls: list[tuple[str, str, str]] = []

    async def fake_stream_unavailable(
        app: Any,
        enriched_text: str,
        sid: str,
        emit_chunk: Any,
        **kwargs: Any,
    ) -> None:
        del enriched_text, emit_chunk, kwargs
        from clio_agent.gact.app import _record_stream_fallback

        _record_stream_fallback(app, sid, "dynamic_prompt_stream_unavailable")
        return None

    def fake_prompt_agent(base_agent: Any, agent_def: Any, question: str, session_id: str) -> Any:
        del base_agent
        calls.append((agent_def.id, question, session_id))
        if agent_def.id == "root_review" and len([row for row in calls if row[0] == "root_review"]) == 1:
            return type(
                "Pred",
                (),
                {
                    "answer": "ROOT_OK",
                    "selected_expert": agent_def.id,
                    "routing_rationale": "session selected expert-pack agent",
                    "next_expert": "schema_review",
                    "next_task": "Inspect the CSV schema",
                },
            )()
        if agent_def.id == "root_review":
            return type(
                "Pred",
                (),
                {
                    "answer": "ROOT_FINAL",
                    "selected_expert": agent_def.id,
                    "routing_rationale": "parent resumed after child expert",
                    "next_expert": "finish",
                },
            )()
        return type(
            "Pred",
            (),
            {
                "answer": "SCHEMA_OK",
                "selected_expert": agent_def.id,
                "routing_rationale": "delegated child expert",
            },
        )()

    monkeypatch.setattr("clio_agent.gact.app._try_streamed_forward", fake_stream_unavailable)
    monkeypatch.setattr("clio_agent.gact.app._run_prompt_user_agent", fake_prompt_agent)

    root = isolated_env / ".clio" / "experts"
    root.mkdir(parents=True)
    root.joinpath("root_review.md").write_text(
        """---
id: root_review
title: Root Review Expert
parent_id: analysis
tier: 2
---
Coordinate data quality review.
""",
        encoding="utf-8",
    )
    root.joinpath("schema_review.md").write_text(
        """---
id: schema_review
title: Schema Review Expert
parent_id: root_review
tier: 3
---
Inspect schemas.
""",
        encoding="utf-8",
    )

    app = build_app(sessions_path=tmp_path / "sessions.json", agent=object())
    with TestClient(app) as client:
        sid = client.post(
            "/v1/sessions",
            json={"title": "delegated expert run", "agent": {"id": "root_review"}},
        ).json()["id"]
        assistant = complete_turn(client, sid, "inspect data.csv")

    assert calls == [
        ("root_review", "inspect data.csv", sid),
        (
            "schema_review",
            (
                "Inspect the CSV schema\n\n"
                "Parent evidence available for this delegated task:\n\n"
                "ROOT_OK"
            ),
            sid,
        ),
        ("root_review", calls[2][1], sid),
    ]
    assert "Returned child expert results" in calls[2][1]
    def iter_handoffs(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        found: list[dict[str, Any]] = []
        stack = list(rows)
        while stack:
            row = stack.pop(0)
            found.append(row)
            children = row.get("children")
            if isinstance(children, list):
                stack.extend(child for child in children if isinstance(child, dict))
        return found

    handoffs = iter_handoffs(assistant["metadata"]["expert_handoffs"])
    handoff = next(row for row in handoffs if row["agent_id"] == "schema_review")
    assert handoff["agent_id"] == "schema_review"
    assert handoff["parent_id"] == "root_review"
    assert handoff["status"] == "completed"
    assert handoff["stage"] == "delegate.completed"
    assert handoff["delegation_lifecycle"] == "sync"
    assert handoff["return_to"] == "root_review"
    assert handoff["execution_mode"] == "prompt_agent"
    assert handoff["output_summary"] == "SCHEMA_OK"
    resumed = next(row for row in handoffs if row.get("stage") == "parent.resumed")
    assert resumed["agent_id"] == "root_review"
    assert resumed["resumed_from"] == "schema_review"
    semantic = [
        event.payload
        for event in app.state.bus._history.get(sid, [])
        if event.type == "semantic.event"
    ]
    semantic_types = [event["event_type"] for event in semantic]
    assert "delegation.started" in semantic_types
    assert "delegation.completed" in semantic_types
    assert "delegation.parent_resumed" in semantic_types
    completed = next(
        event for event in semantic if event["event_type"] == "delegation.completed"
    )
    resumed_event = next(
        event for event in semantic if event["event_type"] == "delegation.parent_resumed"
    )
    assert completed["actor"]["agent_id"] == "schema_review"
    assert completed["subject"]["agent_id"] == "root_review"
    assert completed["payload"]["parent_id"] == "root_review"
    assert resumed_event["actor"]["agent_id"] == "root_review"
    assert resumed_event["subject"]["agent_id"] == "schema_review"
    assert resumed_event["payload"]["resumed_from"] == "schema_review"
    assert assistant["parts"][1]["type"] == "expert_handoff"
    assert assistant["parts"][1]["metadata"]["status"] == "completed"
    assert assistant["parts"][-1]["text"] == "ROOT_FINAL"


def test_returned_continuation_contract_executes_declared_child(
    isolated_env: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from .conftest import complete_turn

    calls: list[tuple[str, str, str]] = []

    async def fake_stream_unavailable(
        app: Any,
        enriched_text: str,
        sid: str,
        emit_chunk: Any,
        **kwargs: Any,
    ) -> None:
        del enriched_text, emit_chunk, kwargs
        from clio_agent.gact.app import _record_stream_fallback

        _record_stream_fallback(app, sid, "dynamic_prompt_stream_unavailable")
        return None

    def fake_prompt_agent(base_agent: Any, agent_def: Any, question: str, session_id: str) -> Any:
        del base_agent
        calls.append((agent_def.id, question, session_id))
        root_calls = len([row for row in calls if row[0] == "root_review"])
        if agent_def.id == "root_review" and root_calls == 1:
            return type(
                "Pred",
                (),
                {
                    "answer": "ROOT_REQUESTED_SCHEMA",
                    "selected_expert": agent_def.id,
                    "routing_rationale": "session selected expert-pack agent",
                    "next_expert": "schema_review",
                    "next_task": "Inspect the CSV schema",
                },
            )()
        if agent_def.id == "schema_review":
            return type(
                "Pred",
                (),
                {
                    "answer": "\n".join(
                        [
                            "SCHEMA_OK",
                            "NEXT_EXPERT: visualization",
                            "NEXT_ACTION: plot_schema_summary data.csv",
                            "DO_NOT_FINALIZE_BEFORE_VISUALIZATION: true",
                        ]
                    ),
                    "selected_expert": agent_def.id,
                    "routing_rationale": "delegated child expert",
                },
            )()
        # root_review resumes after schema_review's return, reads the returned
        # NEXT_EXPERT/NEXT_ACTION evidence, and routes to visualization itself
        # via its typed next_expert (agent-driven routing replaced the deterministic
        # continuation-contract auto-injection).
        if agent_def.id == "root_review" and root_calls == 2:
            assert "SCHEMA_OK" in question
            assert "plot_schema_summary data.csv" in question
            return type(
                "Pred",
                (),
                {
                    "answer": "ROOT_ROUTING_TO_VISUALIZATION",
                    "selected_expert": agent_def.id,
                    "routing_rationale": "route to visualization per returned evidence",
                    "next_expert": "visualization",
                    "next_task": "plot_schema_summary data.csv",
                },
            )()
        if agent_def.id == "visualization":
            assert "plot_schema_summary data.csv" in question
            return type(
                "Pred",
                (),
                {
                    "answer": "FINAL_ARTIFACT: /tmp/schema.png",
                    "selected_expert": agent_def.id,
                    "routing_rationale": "returned continuation contract",
                },
            )()
        return type(
            "Pred",
            (),
            {
                "answer": "ROOT_FINAL",
                "selected_expert": agent_def.id,
                "routing_rationale": "parent resumed after continuation child",
                "next_expert": "finish",
            },
        )()

    monkeypatch.setattr("clio_agent.gact.app._try_streamed_forward", fake_stream_unavailable)
    monkeypatch.setattr("clio_agent.gact.app._run_prompt_user_agent", fake_prompt_agent)

    root = isolated_env / ".clio" / "experts"
    root.mkdir(parents=True)
    root.joinpath("root_review.md").write_text(
        """---
id: root_review
title: Root Review Expert
parent_id: analysis
tier: 2
children:
  - schema_review
  - visualization
---
Coordinate data quality review.
""",
        encoding="utf-8",
    )
    root.joinpath("schema_review.md").write_text(
        """---
id: schema_review
title: Schema Review Expert
parent_id: root_review
tier: 3
---
Inspect schemas.
""",
        encoding="utf-8",
    )
    root.joinpath("visualization.md").write_text(
        """---
id: visualization
title: Visualization Expert
parent_id: root_review
tier: 3
---
Produce artifacts.
""",
        encoding="utf-8",
    )

    app = build_app(sessions_path=tmp_path / "sessions.json", agent=object())
    with TestClient(app) as client:
        sid = client.post(
            "/v1/sessions",
            json={"title": "delegated expert run", "agent": {"id": "root_review"}},
        ).json()["id"]
        assistant = complete_turn(client, sid, "inspect data.csv and plot a summary")

    assert [row[0] for row in calls] == [
        "root_review",
        "schema_review",
        "root_review",
        "visualization",
        "root_review",
    ]
    def iter_handoffs(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        found: list[dict[str, Any]] = []
        stack = list(rows)
        while stack:
            row = stack.pop(0)
            found.append(row)
            children = row.get("children")
            if isinstance(children, list):
                stack.extend(child for child in children if isinstance(child, dict))
        return found

    handoffs = iter_handoffs(assistant["metadata"]["expert_handoffs"])
    visualization = next(
        row
        for row in handoffs
        if row.get("agent_id") == "visualization"
        and row.get("stage") == "delegate.completed"
    )
    # The parent routes to visualization via its typed next_expert after reading
    # schema_review's returned evidence, so the executed handoff is tagged
    # agent_next_expert (the deterministic continuation-contract injection that
    # used to tag this delegation_continuation_contract was removed).
    assert visualization["source"] == "agent_next_expert"
    assert visualization["output_summary"] == "FINAL_ARTIFACT: /tmp/schema.png"
    assert assistant["parts"][-1]["text"] == "ROOT_FINAL"


def test_returned_continuation_contract_rejects_non_child_target(
    isolated_env: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from .conftest import complete_turn

    calls: list[str] = []

    async def fake_stream_unavailable(
        app: Any,
        enriched_text: str,
        sid: str,
        emit_chunk: Any,
        **kwargs: Any,
    ) -> None:
        del enriched_text, emit_chunk, kwargs
        from clio_agent.gact.app import _record_stream_fallback

        _record_stream_fallback(app, sid, "dynamic_prompt_stream_unavailable")
        return None

    def fake_prompt_agent(base_agent: Any, agent_def: Any, question: str, session_id: str) -> Any:
        del base_agent, question, session_id
        calls.append(agent_def.id)
        if agent_def.id == "root_review" and calls.count("root_review") == 1:
            return type(
                "Pred",
                (),
                {
                    "answer": "ROOT_REQUESTED_SCHEMA",
                    "selected_expert": agent_def.id,
                    "routing_rationale": "session selected expert-pack agent",
                    "next_expert": "schema_review",
                    "next_task": "Inspect the CSV schema",
                },
            )()
        if agent_def.id == "schema_review":
            return type(
                "Pred",
                (),
                {
                    "answer": "NEXT_EXPERT: outside_agent\nNEXT_ACTION: should_not_run",
                    "selected_expert": agent_def.id,
                    "routing_rationale": "delegated child expert",
                },
            )()
        if agent_def.id == "outside_agent":
            raise AssertionError("non-child continuation target must not execute")
        # Parent resume attempts to route to a non-child target; the settle loop
        # must reject next_expert values outside its declared children and finalize.
        return type(
            "Pred",
            (),
            {
                "answer": "ROOT_FINAL",
                "selected_expert": agent_def.id,
                "routing_rationale": "parent resumed after ignored contract",
                "next_expert": "outside_agent",
                "next_task": "should_not_run",
            },
        )()

    monkeypatch.setattr("clio_agent.gact.app._try_streamed_forward", fake_stream_unavailable)
    monkeypatch.setattr("clio_agent.gact.app._run_prompt_user_agent", fake_prompt_agent)

    root = isolated_env / ".clio" / "experts"
    root.mkdir(parents=True)
    root.joinpath("root_review.md").write_text(
        """---
id: root_review
title: Root Review Expert
parent_id: analysis
tier: 2
children:
  - schema_review
---
Coordinate data quality review.
""",
        encoding="utf-8",
    )
    root.joinpath("schema_review.md").write_text(
        """---
id: schema_review
title: Schema Review Expert
parent_id: root_review
tier: 3
---
Inspect schemas.
""",
        encoding="utf-8",
    )
    root.joinpath("outside_agent.md").write_text(
        """---
id: outside_agent
title: Outside Expert
parent_id: other_parent
tier: 3
---
Must not be callable by root_review.
""",
        encoding="utf-8",
    )

    app = build_app(sessions_path=tmp_path / "sessions.json", agent=object())
    with TestClient(app) as client:
        sid = client.post(
            "/v1/sessions",
            json={"title": "delegated expert run", "agent": {"id": "root_review"}},
        ).json()["id"]
        assistant = complete_turn(client, sid, "inspect data.csv")

    assert calls == ["root_review", "schema_review", "root_review"]
    assert assistant["parts"][-1]["text"] == "ROOT_FINAL"


def test_nested_child_evidence_survives_empty_parent_resume(
    isolated_env: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from .conftest import complete_turn

    calls: list[str] = []

    async def fake_stream_unavailable(
        app: Any,
        enriched_text: str,
        sid: str,
        emit_chunk: Any,
        **kwargs: Any,
    ) -> None:
        del enriched_text, emit_chunk, kwargs
        from clio_agent.gact.app import _record_stream_fallback

        _record_stream_fallback(app, sid, "dynamic_prompt_stream_unavailable")
        return None

    def fake_prompt_agent(base_agent: Any, agent_def: Any, question: str, session_id: str) -> Any:
        del base_agent, session_id
        calls.append(agent_def.id)
        if agent_def.id == "root_review" and calls.count("root_review") == 1:
            return type(
                "Pred",
                (),
                {
                    "answer": "ROOT_REQUESTED_DATA",
                    "selected_expert": agent_def.id,
                    "routing_rationale": "session selected expert-pack agent",
                    "next_expert": "data",
                    "next_task": "Find bounded waveform data",
                },
            )()
        if agent_def.id == "data" and calls.count("data") == 1:
            return type(
                "Pred",
                (),
                {
                    "answer": "DATA_REQUESTED_NDP",
                    "selected_expert": agent_def.id,
                    "routing_rationale": "delegated child expert",
                    "next_expert": "ndp_catalog",
                    "next_task": "Stage bounded NDP resource",
                },
            )()
        if agent_def.id == "ndp_catalog":
            return type(
                "Pred",
                (),
                {
                    "answer": "NDP staging failed: resource exceeds allowed staging limit.",
                    "selected_expert": agent_def.id,
                    "routing_rationale": "delegated child expert",
                },
            )()
        # data resumes after ndp_catalog returned the staging-limit blocker. data
        # surfaces the nested blocker evidence as its own answer and finishes (the
        # blocker is terminal for data); the staging-limit evidence reaches root.
        if agent_def.id == "data":
            assert "staging limit" in question
            return type(
                "Pred",
                (),
                {
                    "answer": "Data discovery blocked: NDP returned a staging limit blocker.",
                    "selected_expert": agent_def.id,
                    "routing_rationale": "data resumed after ndp blocker",
                    "next_expert": "finish",
                },
            )()
        if agent_def.id == "fallback_analysis":
            return type(
                "Pred",
                (),
                {
                    "answer": "ANALYSIS_USED_FALLBACK",
                    "selected_expert": agent_def.id,
                    "routing_rationale": "continued from data blocker",
                },
            )()
        # root resumes after data returned the staging-limit blocker and routes to
        # fallback_analysis itself; on the next resume it finalizes.
        if agent_def.id == "root_review" and calls.count("fallback_analysis") == 0:
            assert "staging limit" in question
            return type(
                "Pred",
                (),
                {
                    "answer": "ROOT_ROUTING_TO_FALLBACK",
                    "selected_expert": agent_def.id,
                    "routing_rationale": "route to fallback analysis",
                    "next_expert": "fallback_analysis",
                    "next_task": "run fallback analysis",
                },
            )()
        return type(
            "Pred",
            (),
            {
                "answer": "ROOT_FINAL",
                "selected_expert": agent_def.id,
                "routing_rationale": "parent resumed after analysis",
                "next_expert": "finish",
            },
        )()

    monkeypatch.setattr("clio_agent.gact.app._try_streamed_forward", fake_stream_unavailable)
    monkeypatch.setattr("clio_agent.gact.app._run_prompt_user_agent", fake_prompt_agent)

    root = isolated_env / ".clio" / "experts"
    root.mkdir(parents=True)
    root.joinpath("root_review.md").write_text(
        """---
id: root_review
title: Root Review Expert
parent_id: analysis
tier: 2
children:
  - data
  - fallback_analysis
---
Coordinate the workflow.
""",
        encoding="utf-8",
    )
    root.joinpath("data.md").write_text(
        """---
id: data
title: Data Expert
parent_id: root_review
tier: 3
children:
  - ndp_catalog
parameters:
  continuation_contracts:
    - id: data_blocker_to_analysis
      when_output_contains:
        - staging limit
      next_expert: fallback_analysis
      next_action: run fallback analysis
---
Discover data and return blockers.
""",
        encoding="utf-8",
    )
    root.joinpath("ndp_catalog.md").write_text(
        """---
id: ndp_catalog
title: NDP Catalog Expert
parent_id: data
tier: 4
---
Stage bounded resources.
""",
        encoding="utf-8",
    )
    root.joinpath("fallback_analysis.md").write_text(
        """---
id: fallback_analysis
title: Analysis Expert
parent_id: root_review
tier: 3
---
Analyze recovered data.
""",
        encoding="utf-8",
    )

    app = build_app(sessions_path=tmp_path / "sessions.json", agent=object())
    with TestClient(app) as client:
        sid = client.post(
            "/v1/sessions",
            json={"title": "nested delegation run", "agent": {"id": "root_review"}},
        ).json()["id"]
        assistant = complete_turn(client, sid, "find waveform data and analyze fallback")

    assert calls == [
        "root_review",
        "data",
        "ndp_catalog",
        "data",
        "root_review",
        "fallback_analysis",
        "root_review",
    ]
    def iter_handoffs(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        found: list[dict[str, Any]] = []
        stack = list(rows)
        while stack:
            row = stack.pop(0)
            found.append(row)
            children = row.get("children")
            if isinstance(children, list):
                stack.extend(child for child in children if isinstance(child, dict))
        return found

    handoffs = iter_handoffs(assistant["metadata"]["expert_handoffs"])
    data = next(
        row
        for row in handoffs
        if row.get("agent_id") == "data" and row.get("stage") == "delegate.completed"
    )
    analysis = next(
        row
        for row in handoffs
        if row.get("agent_id") == "fallback_analysis"
        and row.get("stage") == "delegate.completed"
    )
    assert data["status"] == "completed"
    # The nested ndp_catalog staging-limit evidence survives data's empty resume
    # and bubbles up as data's completed output.
    assert "staging limit" in data["output_summary"]
    # root routes to fallback_analysis via its typed next_expert after reading
    # data's bubbled blocker, so the executed handoff is tagged agent_next_expert
    # (the deterministic agent_blueprint_continuation_policy auto-injection was
    # removed).
    assert analysis["source"] == "agent_next_expert"
    assert assistant["parts"][-1]["text"] == "ROOT_FINAL"


def test_tool_agent_preserves_trajectory_evidence_when_answer_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import dspy

    from clio_agent.gact.app import _build_tool_user_agent_module

    class FakeReact:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self.extract = SimpleNamespace(predict=lambda *a, **k: None)

        def __call__(self, *args: Any, **kwargs: Any) -> Any:
            return SimpleNamespace(
                answer="",
                trajectory={
                    "step_0_tool_name": "hdf5_list_datasets",
                    "step_0_observation": {
                        "datasets": ["/sim/temperature", "/sim/pressure"],
                        "warnings": [],
                    },
                },
            )

    monkeypatch.setattr(dspy, "ReAct", FakeReact)
    monkeypatch.setattr(dspy, "context", lambda **kwargs: nullcontext())
    monkeypatch.setattr("clio_agent.config.create_lm", lambda config: object())
    monkeypatch.setattr("clio_agent.config.create_chat_adapter", lambda config: object())

    base_agent = SimpleNamespace(
        tool_executor=SimpleNamespace(
            to_dspy_tools=lambda: [SimpleNamespace(name="hdf5_list_datasets")]
        )
    )
    agent_def = AgentDef(
        id="source_inspect",
        source="expert_pack",
        title="Source Inspect",
        tools=["hdf5_list_datasets"],
    )

    module = _build_tool_user_agent_module(base_agent, agent_def)
    pred = module.forward(question="inspect source", session_id="sess_test")

    assert pred.selected_expert == "source_inspect"
    assert "produced no final prose answer" in pred.answer
    assert "hdf5_list_datasets" not in pred.answer
    assert "/sim/temperature" in pred.answer
    assert pred.trajectory["step_0_tool_name"] == "hdf5_list_datasets"


def test_prompt_agent_empty_answer_with_children_enters_repair_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import dspy

    from clio_agent.gact.app import (
        _ACTIVE_GACT_APP,
        _ACTIVE_GACT_SESSION_ID,
        _build_prompt_user_agent_module,
    )

    class FakePredict:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def __call__(self, *args: Any, **kwargs: Any) -> Any:
            return SimpleNamespace(answer="", expert_handoffs=[])

    monkeypatch.setattr(dspy, "Predict", FakePredict)
    monkeypatch.setattr(dspy, "context", lambda **kwargs: nullcontext())
    monkeypatch.setattr("clio_agent.config.create_lm", lambda config: object())
    monkeypatch.setattr("clio_agent.config.create_chat_adapter", lambda config: object())
    monkeypatch.setattr(
        "clio_agent.gact.app._runtime_dynamic_agent_children_context",
        lambda *args, **kwargs: "Declared child experts available for synchronous delegation:\n- mass_spec",
    )

    agent_def = AgentDef(
        id="main",
        source="expert_pack",
        title="Proteomics Root",
    )

    app_token = _ACTIVE_GACT_APP.set(object())
    session_token = _ACTIVE_GACT_SESSION_ID.set("sess_test")
    try:
        module = _build_prompt_user_agent_module(object(), agent_def)
        pred = module.forward(question="review mzML", session_id="sess_test")
    finally:
        _ACTIVE_GACT_SESSION_ID.reset(session_token)
        _ACTIVE_GACT_APP.reset(app_token)

    assert pred.selected_expert == "main"
    assert pred.answer == ""
    assert pred.expert_handoffs == []
    assert "handoff repair" in pred.routing_rationale




def test_tool_agent_empty_answer_without_observation_still_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import dspy

    from clio_agent.gact.app import _build_tool_user_agent_module

    class FakeReact:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self.extract = SimpleNamespace(predict=lambda *a, **k: None)

        def __call__(self, *args: Any, **kwargs: Any) -> Any:
            return SimpleNamespace(
                answer="",
                trajectory={"step_0_tool_name": "hdf5_list_datasets"},
            )

    monkeypatch.setattr(dspy, "ReAct", FakeReact)
    monkeypatch.setattr(dspy, "context", lambda **kwargs: nullcontext())
    monkeypatch.setattr("clio_agent.config.create_lm", lambda config: object())
    monkeypatch.setattr("clio_agent.config.create_chat_adapter", lambda config: object())

    base_agent = SimpleNamespace(
        tool_executor=SimpleNamespace(
            to_dspy_tools=lambda: [SimpleNamespace(name="hdf5_list_datasets")]
        )
    )
    agent_def = AgentDef(
        id="source_inspect",
        source="expert_pack",
        title="Source Inspect",
        tools=["hdf5_list_datasets"],
    )

    module = _build_tool_user_agent_module(base_agent, agent_def)
    with pytest.raises(RuntimeError, match="returned an empty answer"):
        module.forward(question="inspect source", session_id="sess_test")


def test_tool_agent_invalid_tool_selection_emits_semantic_event(
    monkeypatch: pytest.MonkeyPatch,
    isolated_env: Path,
) -> None:
    import dspy

    from clio_agent.gact.app import (
        _ACTIVE_GACT_APP,
        _ACTIVE_GACT_SESSION_ID,
        _ACTIVE_GACT_TRACE_ID,
        _ACTIVE_GACT_TURN_ID,
        _build_tool_user_agent_module,
    )

    class FakeReact:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self.extract = SimpleNamespace(predict=lambda *a, **k: None)

        def __call__(self, *args: Any, **kwargs: Any) -> Any:
            raise ValueError(
                "Failed to parse field next_tool_name with value shell_bash. "
                "'shell_bash' is not one of ('genomics_summarize_vcf', 'finish')"
            )

    monkeypatch.setattr(dspy, "ReAct", FakeReact)
    monkeypatch.setattr(dspy, "context", lambda **kwargs: nullcontext())
    monkeypatch.setattr("clio_agent.config.create_lm", lambda config: object())
    monkeypatch.setattr("clio_agent.config.create_chat_adapter", lambda config: object())

    app = build_app()
    base_agent = SimpleNamespace(
        tool_executor=SimpleNamespace(
            to_dspy_tools=lambda: [SimpleNamespace(name="genomics_summarize_vcf")]
        )
    )
    agent_def = AgentDef(
        id="variant_impact",
        source="expert_pack",
        title="Variant Impact",
        parent_id="genomics",
        tier=3,
        tools=["genomics_summarize_vcf"],
        default_provider="alcf",
        default_model="metis",
    )

    app_token = _ACTIVE_GACT_APP.set(app)
    session_token = _ACTIVE_GACT_SESSION_ID.set("sess_invalid_tool")
    turn_token = _ACTIVE_GACT_TURN_ID.set("msg_invalid_tool")
    trace_token = _ACTIVE_GACT_TRACE_ID.set("trace_msg_invalid_tool")
    try:
        module = _build_tool_user_agent_module(base_agent, agent_def)
        with pytest.raises(ValueError, match="shell_bash"):
            module.forward(question="summarize variants", session_id="sess_invalid_tool")
    finally:
        _ACTIVE_GACT_TRACE_ID.reset(trace_token)
        _ACTIVE_GACT_TURN_ID.reset(turn_token)
        _ACTIVE_GACT_SESSION_ID.reset(session_token)
        _ACTIVE_GACT_APP.reset(app_token)

    history = app.state.bus._history["sess_invalid_tool"]
    direct = [event for event in history if event.type == "tool.selection.invalid"]
    semantic = [
        event
        for event in history
        if event.type == "semantic.event"
        and event.payload.get("event_type") == "tool.selection.invalid"
    ]

    assert len(direct) == 1
    assert len(semantic) == 1
    assert direct[0].payload["agent_id"] == "variant_impact"
    assert direct[0].payload["requested_tool"] == "shell_bash"
    assert direct[0].payload["allowed_tools"] == ["genomics_summarize_vcf"]
    assert direct[0].payload["tool_executed"] is False
    assert direct[0].payload["trace_id"] == "trace_msg_invalid_tool"

    assert semantic[0].payload["status"] == "failed"
    assert semantic[0].payload["trace_id"] == "trace_msg_invalid_tool"
    assert semantic[0].payload["actor"]["agent_id"] == "variant_impact"
    assert semantic[0].payload["payload"]["requested_tool"] == "shell_bash"
    assert semantic[0].payload["payload"]["tool_executed"] is False
    assert not any(event.type == "tool.call.started" for event in history)


def test_current_expert_continuation_policy_executes_declared_child(
    isolated_env: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from .conftest import complete_turn

    calls: list[str] = []

    async def fake_stream_unavailable(
        app: Any,
        enriched_text: str,
        sid: str,
        emit_chunk: Any,
        **kwargs: Any,
    ) -> None:
        del enriched_text, emit_chunk, kwargs
        from clio_agent.gact.app import _record_stream_fallback

        _record_stream_fallback(app, sid, "dynamic_prompt_stream_unavailable")
        return None

    def fake_prompt_agent(base_agent: Any, agent_def: Any, question: str, session_id: str) -> Any:
        del base_agent, question, session_id
        calls.append(agent_def.id)
        if agent_def.id == "root_review" and calls.count("root_review") == 1:
            return type(
                "Pred",
                (),
                {
                    "answer": "ROOT_REQUESTED_STRUCTURE",
                    "selected_expert": agent_def.id,
                    "routing_rationale": "session selected expert-pack agent",
                    "next_expert": "structure",
                    "next_task": "Inspect CIF",
                },
            )()
        # structure is itself an orchestrator (it has the child `quality`): on its
        # first run it routes to quality via its own typed next_expert; on resume
        # (after quality returns) it finishes. The pack continuation policy is
        # surfaced to the orchestrator, which decides — the deterministic policy
        # auto-injection was removed.
        if agent_def.id == "structure" and calls.count("structure") == 1:
            return type(
                "Pred",
                (),
                {
                    "answer": "Formula SrTiO3 and occupancy evidence are clean.",
                    "selected_expert": agent_def.id,
                    "routing_rationale": "tool-grounded structure answer; review quality next",
                    "next_expert": "quality",
                    "next_task": "review quality",
                },
            )()
        if agent_def.id == "structure":
            return type(
                "Pred",
                (),
                {
                    "answer": "Formula SrTiO3 and occupancy evidence are clean.",
                    "selected_expert": agent_def.id,
                    "routing_rationale": "structure resumed after quality",
                    "next_expert": "finish",
                },
            )()
        if agent_def.id == "quality":
            return type(
                "Pred",
                (),
                {
                    "answer": "QUALITY_REVIEW_DONE",
                    "selected_expert": agent_def.id,
                    "routing_rationale": "policy child",
                },
            )()
        return type(
            "Pred",
            (),
            {
                "answer": "ROOT_FINAL",
                "selected_expert": agent_def.id,
                "routing_rationale": "parent resumed after policy child",
                "next_expert": "finish",
            },
        )()

    monkeypatch.setattr("clio_agent.gact.app._try_streamed_forward", fake_stream_unavailable)
    monkeypatch.setattr("clio_agent.gact.app._run_prompt_user_agent", fake_prompt_agent)

    root = isolated_env / ".clio" / "experts"
    root.mkdir(parents=True)
    root.joinpath("root_review.md").write_text(
        """---
id: root_review
title: Root Review Expert
parent_id: analysis
tier: 2
children:
  - structure
---
Coordinate review.
""",
        encoding="utf-8",
    )
    root.joinpath("structure.md").write_text(
        """---
id: structure
title: Structure Expert
parent_id: root_review
tier: 3
children:
  - quality
parameters:
  continuation_contracts:
    - id: structure_to_quality
      when_output_contains:
        - occupancy
      next_expert: quality
      next_action: review quality
---
Inspect structure.
""",
        encoding="utf-8",
    )
    root.joinpath("quality.md").write_text(
        """---
id: quality
title: Quality Expert
parent_id: structure
tier: 4
---
Review quality.
""",
        encoding="utf-8",
    )

    app = build_app(sessions_path=tmp_path / "sessions.json", agent=object())
    with TestClient(app) as client:
        sid = client.post(
            "/v1/sessions",
            json={"title": "current policy run", "agent": {"id": "root_review"}},
        ).json()["id"]
        assistant = complete_turn(client, sid, "review the CIF")

    assert calls == ["root_review", "structure", "quality", "structure", "root_review"]
    def iter_handoffs(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        found: list[dict[str, Any]] = []
        stack = list(rows)
        while stack:
            row = stack.pop(0)
            found.append(row)
            children = row.get("children")
            if isinstance(children, list):
                stack.extend(child for child in children if isinstance(child, dict))
        return found

    handoffs = iter_handoffs(assistant["metadata"]["expert_handoffs"])
    quality = next(
        row
        for row in handoffs
        if row.get("agent_id") == "quality" and row.get("stage") == "delegate.completed"
    )
    # structure routes to quality via its own typed next_expert, so the executed
    # handoff is tagged agent_next_expert (the deterministic
    # agent_blueprint_continuation_policy auto-injection was removed).
    assert quality["source"] == "agent_next_expert"
    assert assistant["parts"][-1]["text"] == "ROOT_FINAL"


def test_continuation_contract_retries_incomplete_prior_child(
    isolated_env: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from .conftest import complete_turn

    calls: list[tuple[str, str]] = []

    async def fake_stream_unavailable(
        app: Any,
        enriched_text: str,
        sid: str,
        emit_chunk: Any,
        **kwargs: Any,
    ) -> None:
        del enriched_text, emit_chunk, kwargs
        from clio_agent.gact.app import _record_stream_fallback

        _record_stream_fallback(app, sid, "dynamic_prompt_stream_unavailable")
        return None

    def fake_prompt_agent(base_agent: Any, agent_def: Any, question: str, session_id: str) -> Any:
        del base_agent, session_id
        calls.append((agent_def.id, question))
        viz_calls = len([row for row in calls if row[0] == "waveform_visualization"])
        analysis_calls = len([row for row in calls if row[0] == "waveform_analysis"])
        if agent_def.id == "root_review" and len(calls) == 1:
            # The orchestrator tries visualization first (premature, before data).
            return type(
                "Pred",
                (),
                {
                    "answer": "ROOT_REQUESTED_ANALYSIS_AND_PLOT",
                    "selected_expert": agent_def.id,
                    "routing_rationale": "session selected expert-pack agent",
                    "next_expert": "waveform_visualization",
                    "next_task": "Generate a PNG plot of the waveform using computed statistics.",
                },
            )()
        if agent_def.id == "waveform_visualization" and viz_calls == 1:
            return type(
                "Pred",
                (),
                {
                    "answer": (
                        "I cannot create the PNG yet because I do not have a SAC file. "
                        "We need to obtain waveform data before plotting."
                    ),
                    "selected_expert": agent_def.id,
                    "routing_rationale": "called before data was available",
                },
            )()
        if agent_def.id == "root_review" and analysis_calls == 0:
            # visualization reported it is blocked on data; route to analysis to
            # recover the SAC evidence.
            return type(
                "Pred",
                (),
                {
                    "answer": "ROOT_ROUTING_TO_ANALYSIS",
                    "selected_expert": agent_def.id,
                    "routing_rationale": "recover SAC evidence first",
                    "next_expert": "waveform_analysis",
                    "next_task": "Recover SAC evidence and trace statistics.",
                },
            )()
        if agent_def.id == "waveform_analysis":
            return type(
                "Pred",
                (),
                {
                    "answer": (
                        "Trace statistics complete for /tmp/waveform.sac.\n\n"
                        "NEXT_EXPERT: waveform_visualization\n"
                        "NEXT_ACTION: plot_sac_traces /tmp/waveform.sac"
                    ),
                    "selected_expert": agent_def.id,
                    "routing_rationale": "analysis recovered SAC evidence",
                },
            )()
        if agent_def.id == "root_review" and viz_calls == 1:
            # analysis returned the SAC evidence; the orchestrator re-routes to
            # visualization to retry it with the recovered SAC path.
            return type(
                "Pred",
                (),
                {
                    "answer": "ROOT_RETRYING_VISUALIZATION",
                    "selected_expert": agent_def.id,
                    "routing_rationale": "retry visualization with returned SAC evidence",
                    "next_expert": "waveform_visualization",
                    "next_task": (
                        "Returned evidence from waveform_analysis: "
                        "plot_sac_traces /tmp/waveform.sac"
                    ),
                },
            )()
        if agent_def.id == "waveform_visualization":
            assert "/tmp/waveform.sac" in question
            return type(
                "Pred",
                (),
                {
                    "answer": "PNG_DONE /tmp/waveform.png",
                    "selected_expert": agent_def.id,
                    "routing_rationale": "retried with returned SAC evidence",
                },
            )()
        return type(
            "Pred",
            (),
            {
                "answer": "ROOT_FINAL",
                "selected_expert": agent_def.id,
                "routing_rationale": "root finalized",
                "next_expert": "finish",
            },
        )()

    monkeypatch.setattr("clio_agent.gact.app._try_streamed_forward", fake_stream_unavailable)
    monkeypatch.setattr("clio_agent.gact.app._run_prompt_user_agent", fake_prompt_agent)

    root = isolated_env / ".clio" / "experts"
    root.mkdir(parents=True)
    root.joinpath("root_review.md").write_text(
        """---
id: root_review
title: Root Review Expert
parent_id: analysis
tier: 2
children:
  - waveform_visualization
  - waveform_analysis
parameters:
  max_sync_delegation_rounds: 4
---
Coordinate analysis and visualization.
""",
        encoding="utf-8",
    )
    root.joinpath("waveform_analysis.md").write_text(
        """---
id: waveform_analysis
title: Analysis Expert
parent_id: root_review
tier: 3
---
Recover SAC evidence.
""",
        encoding="utf-8",
    )
    root.joinpath("waveform_visualization.md").write_text(
        """---
id: waveform_visualization
title: Visualization Expert
parent_id: root_review
tier: 3
---
Plot SAC artifacts.
""",
        encoding="utf-8",
    )

    app = build_app(sessions_path=tmp_path / "sessions.json", agent=object())
    with TestClient(app) as client:
        sid = client.post(
            "/v1/sessions",
            json={"title": "retry continuation", "agent": {"id": "root_review"}},
        ).json()["id"]
        assistant = complete_turn(client, sid, "recover waveform and plot it")

    assert calls, assistant
    visualization_calls = [
        question for agent_id, question in calls if agent_id == "waveform_visualization"
    ]
    assert len(visualization_calls) == 2
    assert "Returned evidence" in visualization_calls[1]
    assert assistant["parts"][-1]["text"] == "ROOT_FINAL"

    def iter_handoffs(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        found: list[dict[str, Any]] = []
        stack = list(rows)
        while stack:
            row = stack.pop(0)
            found.append(row)
            children = row.get("children")
            if isinstance(children, list):
                stack.extend(child for child in children if isinstance(child, dict))
        return found

    handoffs = iter_handoffs(assistant["metadata"]["expert_handoffs"])
    visualization_completed = [
        row
        for row in handoffs
        if row.get("agent_id") == "waveform_visualization"
        and row.get("stage") == "delegate.completed"
    ]
    assert [row["output_summary"] for row in visualization_completed] == [
        (
            "I cannot create the PNG yet because I do not have a SAC file. "
            "We need to obtain waveform data before plotting."
        ),
        "PNG_DONE /tmp/waveform.png",
    ]
    assert not any(
        row.get("agent_id") == "waveform_visualization"
        and row.get("stage") == "delegate.skipped"
        for row in handoffs
    )


def test_pack_continuation_policy_executes_declared_child_when_contract_text_missing(
    isolated_env: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from .conftest import complete_turn

    calls: list[tuple[str, str]] = []

    async def fake_stream_unavailable(
        app: Any,
        enriched_text: str,
        sid: str,
        emit_chunk: Any,
        **kwargs: Any,
    ) -> None:
        del enriched_text, emit_chunk, kwargs
        from clio_agent.gact.app import _record_stream_fallback

        _record_stream_fallback(app, sid, "dynamic_prompt_stream_unavailable")
        return None

    def fake_prompt_agent(base_agent: Any, agent_def: Any, question: str, session_id: str) -> Any:
        del base_agent, session_id
        calls.append((agent_def.id, question))
        if agent_def.id == "root_review" and len([row for row in calls if row[0] == "root_review"]) == 1:
            return type(
                "Pred",
                (),
                {
                    "answer": "ROOT_REQUESTED_SCHEMA",
                    "selected_expert": agent_def.id,
                    "routing_rationale": "session selected expert-pack agent",
                    "next_expert": "schema_review",
                    "next_task": "Inspect the CSV schema",
                },
            )()
        if agent_def.id == "schema_review":
            return type(
                "Pred",
                (),
                {
                    "answer": "Schema source returned resource_too_large before sampling.",
                    "selected_expert": agent_def.id,
                    "routing_rationale": "delegated child expert",
                },
            )()
        # root_review resumes after schema_review's resource_too_large return and
        # routes to visualization via its typed next_expert (the pack-defined
        # continuation policy is surfaced to the orchestrator, which decides).
        if agent_def.id == "root_review" and len([row for row in calls if row[0] == "root_review"]) == 2:
            assert "resource_too_large" in question
            return type(
                "Pred",
                (),
                {
                    "answer": "ROOT_ROUTING_TO_VISUALIZATION",
                    "selected_expert": agent_def.id,
                    "routing_rationale": "recover via visualization",
                    "next_expert": "visualization",
                    "next_task": "plot_schema_summary data.csv",
                },
            )()
        if agent_def.id == "visualization":
            assert "plot_schema_summary data.csv" in question
            return type(
                "Pred",
                (),
                {
                    "answer": "FINAL_ARTIFACT: /tmp/schema.png",
                    "selected_expert": agent_def.id,
                    "routing_rationale": "pack-defined continuation policy",
                },
            )()
        return type(
            "Pred",
            (),
            {
                "answer": "ROOT_FINAL",
                "selected_expert": agent_def.id,
                "routing_rationale": "parent resumed after continuation child",
                "next_expert": "finish",
            },
        )()

    monkeypatch.setattr("clio_agent.gact.app._try_streamed_forward", fake_stream_unavailable)
    monkeypatch.setattr("clio_agent.gact.app._run_prompt_user_agent", fake_prompt_agent)

    root = isolated_env / ".clio" / "experts"
    root.mkdir(parents=True)
    root.joinpath("root_review.md").write_text(
        """---
id: root_review
title: Root Review Expert
parent_id: analysis
tier: 2
children:
  - schema_review
  - visualization
parameters:
  max_sync_delegation_rounds: 4
---
Coordinate data quality review.
""",
        encoding="utf-8",
    )
    root.joinpath("schema_review.md").write_text(
        """---
id: schema_review
title: Schema Review Expert
parent_id: root_review
tier: 3
parameters:
  continuation_contracts:
    - id: schema_too_large_recovery
      when_output_contains:
        - resource_too_large
      next_expert: visualization
      next_action: plot_schema_summary data.csv
      flags:
        DO_NOT_FINALIZE_BEFORE_VISUALIZATION: "true"
---
Inspect schemas.
""",
        encoding="utf-8",
    )
    root.joinpath("visualization.md").write_text(
        """---
id: visualization
title: Visualization Expert
parent_id: root_review
tier: 3
---
Produce artifacts.
""",
        encoding="utf-8",
    )

    app = build_app(sessions_path=tmp_path / "sessions.json", agent=object())
    with TestClient(app) as client:
        sid = client.post(
            "/v1/sessions",
            json={"title": "delegated expert run", "agent": {"id": "root_review"}},
        ).json()["id"]
        assistant = complete_turn(client, sid, "inspect data.csv and plot a summary")

    assert [row[0] for row in calls] == [
        "root_review",
        "schema_review",
        "root_review",
        "visualization",
        "root_review",
    ]
    handoffs = assistant["metadata"]["expert_handoffs"]
    visualization = next(
        row
        for row in handoffs
        if row.get("agent_id") == "visualization"
        and row.get("stage") == "delegate.completed"
    )
    # The orchestrator routes to visualization via its typed next_expert, so the
    # executed handoff is tagged agent_next_expert (the deterministic
    # agent_blueprint_continuation_policy auto-injection was removed).
    assert visualization["source"] == "agent_next_expert"
    assert visualization["output_summary"] == "FINAL_ARTIFACT: /tmp/schema.png"
    assert assistant["parts"][-1]["text"] == "ROOT_FINAL"


def test_pack_continuation_policy_rejects_non_child_target(
    isolated_env: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from .conftest import complete_turn

    calls: list[str] = []

    async def fake_stream_unavailable(
        app: Any,
        enriched_text: str,
        sid: str,
        emit_chunk: Any,
        **kwargs: Any,
    ) -> None:
        del enriched_text, emit_chunk, kwargs
        from clio_agent.gact.app import _record_stream_fallback

        _record_stream_fallback(app, sid, "dynamic_prompt_stream_unavailable")
        return None

    def fake_prompt_agent(base_agent: Any, agent_def: Any, question: str, session_id: str) -> Any:
        del base_agent, question, session_id
        calls.append(agent_def.id)
        if agent_def.id == "root_review" and calls.count("root_review") == 1:
            return type(
                "Pred",
                (),
                {
                    "answer": "ROOT_REQUESTED_SCHEMA",
                    "selected_expert": agent_def.id,
                    "routing_rationale": "session selected expert-pack agent",
                    "next_expert": "schema_review",
                    "next_task": "Inspect the CSV schema",
                },
            )()
        if agent_def.id == "schema_review":
            return type(
                "Pred",
                (),
                {
                    "answer": "Schema source returned resource_too_large before sampling.",
                    "selected_expert": agent_def.id,
                    "routing_rationale": "delegated child expert",
                },
            )()
        if agent_def.id == "outside_agent":
            raise AssertionError("non-child continuation policy target must not execute")
        # Parent resume attempts to route to a non-child target; the settle loop
        # must reject next_expert values outside its declared children and finalize.
        return type(
            "Pred",
            (),
            {
                "answer": "ROOT_FINAL",
                "selected_expert": agent_def.id,
                "routing_rationale": "parent resumed after ignored policy",
                "next_expert": "outside_agent",
                "next_task": "should_not_run",
                "expert_handoffs": [],
            },
        )()

    monkeypatch.setattr("clio_agent.gact.app._try_streamed_forward", fake_stream_unavailable)
    monkeypatch.setattr("clio_agent.gact.app._run_prompt_user_agent", fake_prompt_agent)

    root = isolated_env / ".clio" / "experts"
    root.mkdir(parents=True)
    root.joinpath("root_review.md").write_text(
        """---
id: root_review
title: Root Review Expert
parent_id: analysis
tier: 2
children:
  - schema_review
---
Coordinate data quality review.
""",
        encoding="utf-8",
    )
    root.joinpath("schema_review.md").write_text(
        """---
id: schema_review
title: Schema Review Expert
parent_id: root_review
tier: 3
parameters:
  continuation_contracts:
    - id: unsafe_recovery
      when_output_contains:
        - resource_too_large
      next_expert: outside_agent
      next_action: should_not_run
---
Inspect schemas.
""",
        encoding="utf-8",
    )
    root.joinpath("outside_agent.md").write_text(
        """---
id: outside_agent
title: Outside Expert
parent_id: other_parent
tier: 3
---
Must not be callable by root_review.
""",
        encoding="utf-8",
    )

    app = build_app(sessions_path=tmp_path / "sessions.json", agent=object())
    with TestClient(app) as client:
        sid = client.post(
            "/v1/sessions",
            json={"title": "delegated expert run", "agent": {"id": "root_review"}},
        ).json()["id"]
        assistant = complete_turn(client, sid, "inspect data.csv")

    assert calls == ["root_review", "schema_review", "root_review"]
    assert assistant["parts"][-1]["text"] == "ROOT_FINAL"


def test_delegated_child_tool_telemetry_stays_on_active_parent_turn(
    isolated_env: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from clio_agent.tools.execution import notify_global_tool_observer

    from .conftest import complete_turn

    calls: list[tuple[str, str, str]] = []

    async def fake_stream_unavailable(
        app: Any,
        enriched_text: str,
        sid: str,
        emit_chunk: Any,
        **kwargs: Any,
    ) -> None:
        del enriched_text, emit_chunk, kwargs
        from clio_agent.gact.app import _record_stream_fallback

        _record_stream_fallback(app, sid, "dynamic_prompt_stream_unavailable")
        return None

    def fake_prompt_agent(base_agent: Any, agent_def: Any, question: str, session_id: str) -> Any:
        del base_agent
        calls.append((agent_def.id, question, session_id))
        root_call_count = len([row for row in calls if row[0] == "root_review"])
        if agent_def.id == "root_review" and root_call_count == 1:
            return type(
                "Pred",
                (),
                {
                    "answer": "ROOT_REQUESTED_SCHEMA",
                    "selected_expert": agent_def.id,
                    "routing_rationale": "session selected expert-pack agent",
                    "next_expert": "schema_review",
                    "next_task": "Inspect the CSV schema",
                },
            )()
        if agent_def.id == "schema_review":
            notify_global_tool_observer("csv_schema_inspect", {"path": "data.csv"}, "started", None)
            notify_global_tool_observer("csv_schema_inspect", {"path": "data.csv"}, "completed", None)
            return type(
                "Pred",
                (),
                {
                    "answer": "SCHEMA_OK",
                    "selected_expert": agent_def.id,
                    "routing_rationale": "delegated child expert",
                },
            )()
        return type(
            "Pred",
            (),
            {
                "answer": "ROOT_FINAL",
                "selected_expert": agent_def.id,
                "routing_rationale": "parent resumed after child expert",
                "next_expert": "finish",
            },
        )()

    monkeypatch.setattr("clio_agent.gact.app._try_streamed_forward", fake_stream_unavailable)
    monkeypatch.setattr("clio_agent.gact.app._run_prompt_user_agent", fake_prompt_agent)

    root = isolated_env / ".clio" / "experts"
    root.mkdir(parents=True)
    root.joinpath("root_review.md").write_text(
        """---
id: root_review
title: Root Review Expert
parent_id: analysis
tier: 2
---
Coordinate data quality review.
""",
        encoding="utf-8",
    )
    root.joinpath("schema_review.md").write_text(
        """---
id: schema_review
title: Schema Review Expert
parent_id: root_review
tier: 3
---
Inspect schemas.
""",
        encoding="utf-8",
    )

    app = build_app(sessions_path=tmp_path / "sessions.json", agent=object())
    with TestClient(app) as client:
        older_sid = client.post(
            "/v1/sessions",
            json={"title": "delegated expert run", "agent": {"id": "root_review"}},
        ).json()["id"]
        newer_sid = client.post(
            "/v1/sessions",
            json={"title": "newer idle", "agent": {"id": "root_review"}},
        ).json()["id"]
        assistant = complete_turn(client, older_sid, "inspect data.csv")

    tools_called = assistant["metadata"]["tools_called"]
    assert [row["name"] for row in tools_called] == ["csv_schema_inspect"]
    assert tools_called[0]["args"] == {"path": "data.csv"}
    assert newer_sid not in app.state.tool_call_ledger
    assert app.state.bus._history.get(newer_sid, []) == []
