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
    validate_expert_hierarchy,
)
from clio_agent.gact.types import AgentDef

# #948 S4b: default sessions run the blueprint react ``main``; route it to each
# test's ``build_app(agent=...)`` host fake.
pytestmark = pytest.mark.usefixtures("host_agent_executor")


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


def test_parse_expert_file_forwards_provider_identity_fields(tmp_path: Path) -> None:
    """Frontmatter api_base/credential_ref/transport parse into AgentDef (#818)."""

    path = tmp_path / "cross_provider.md"
    path.write_text(
        """---
id: cross_provider
title: Cross Provider Expert
parent_id: main
tier: 2
provider: openai
model: gpt-5.1
api_base: https://alt.example.com/v1
credential_ref: openai:acctB
transport: exec
prompt_id: clio.expert.cross_provider
---
Use a second provider account.
""",
        encoding="utf-8",
    )

    row = parse_expert_file(path, scope="workspace")

    assert row.default_provider == "openai"
    assert row.default_model == "gpt-5.1"
    assert row.api_base == "https://alt.example.com/v1"
    assert row.credential_ref == "openai:acctB"
    assert row.transport == "exec"


def test_parse_expert_file_provider_identity_defaults_empty(tmp_path: Path) -> None:
    """Absent api_base/credential_ref/transport default to '' (unchanged meaning)."""

    path = tmp_path / "plain.md"
    path.write_text(
        """---
id: plain_expert
title: Plain Expert
parent_id: main
tier: 2
prompt_id: clio.expert.plain
---
No provider overrides.
""",
        encoding="utf-8",
    )

    row = parse_expert_file(path, scope="workspace")

    assert row.api_base == ""
    assert row.credential_ref == ""
    assert row.transport == ""


def test_agent_def_provider_identity_fields_default_empty() -> None:
    """A bare AgentDef defaults the new provider-identity fields to '' (backward-compat)."""

    row = AgentDef(id="bare", title="Bare")

    assert row.api_base == ""
    assert row.credential_ref == ""
    assert row.transport == ""


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


def _hierarchy_row(expert_id: str, *, parent_id: str = "", kind: str = "") -> AgentDef:
    return AgentDef(
        id=expert_id,
        source="expert_pack",
        title=expert_id,
        parent_id=parent_id,
        system_prompt="do the work",
        module={"kind": kind} if kind else {},
        tier=2 if parent_id else 1,
    )


@pytest.mark.parametrize(
    ("parent_kind", "expect_error"),
    [
        ("react", False),
        ("chain_of_thought", True),
        ("predict", True),
        ("", True),  # module.kind defaults to predict — still cannot reach children
    ],
)
def test_parent_with_children_must_be_react(parent_kind: str, expect_error: bool) -> None:
    """#948 S4: children are reachable only via the spawn-runtime tools (react)."""

    rows = validate_expert_hierarchy(
        [
            _hierarchy_row("main", kind=parent_kind),
            _hierarchy_row("child", parent_id="main", kind="chain_of_thought"),
        ]
    )
    parent = next(row for row in rows if row.id == "main")
    child = next(row for row in rows if row.id == "child")

    if expect_error:
        assert parent.enabled is False
        assert any("declare module.kind: react" in error for error in parent.validation_errors)
    else:
        assert parent.enabled is True
        assert parent.validation_errors == []
    # A LEAF predict/chain_of_thought expert stays valid in every case.
    assert child.enabled is True
    assert child.validation_errors == []


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
parent_id: main
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
    """A loose workspace expert augments the (bare-session-honest) hierarchy.

    Declares ``parent_id: main`` -- the ONLY parent genuinely available for an
    unactivated session post-fix (``catalog._builtin_agents()`` no longer
    implicitly loads the installed default-registry snapshot's "analysis"/
    "sac_format" rows; the loose expert must hang off the code-shipped
    builtin main directly, matching the "augment the resolved default main"
    contract the loose-expert lane always intended, #770 C1).
    """

    root = isolated_env / ".clio" / "experts"
    root.mkdir(parents=True)
    root.joinpath("csv_quality.md").write_text(
        """---
id: csv_quality
title: CSV Quality Expert
description: Checks CSV quality
parent_id: main
tier: 2
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

    assert rows["main"]["source"] == "builtin"
    expert = rows["csv_quality"]
    assert expert["source"] == "expert_pack"
    assert expert["parent_id"] == "main"
    assert expert["tier"] == 2
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
    assert (
        client.get(f"/v1/sessions/{sid_data}/expert-pack").json()["active_expert_pack_id"]
        == "data-semantics"
    )

    data_agents = {
        row["id"]
        for row in client.get("/v1/agents", params={"session_id": sid_data}).json()["agents"]
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
        row["id"]
        for row in client.get("/v1/expert-packs", params={"workspace_id": wid_a}).json()[
            "expert_packs"
        ]
    }
    packs_b = {
        row["id"]
        for row in client.get("/v1/expert-packs", params={"workspace_id": wid_b}).json()[
            "expert_packs"
        ]
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
        # parent_id: main -- the ONLY parent genuinely available for an
        # unactivated session (catalog._builtin_agents() no longer implicitly
        # loads the installed default-registry snapshot's "analysis" row).
        assert agent_def.parent_id == "main"
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
parent_id: main
tier: 2
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
    # Text answer only — the routing decision rides the semantic highway as a
    # routing.decision event (a0e1d9a9), never a message part.
    assert [part["type"] for part in assistant["parts"]] == ["text"]
    assert assistant["parts"][0]["text"] == "CSV_QUALITY_OK"
    routing_events = [
        ev
        for ev in app.state.bus._history.get(sid, [])
        if ev.type == "semantic.event" and ev.payload.get("event_type") == "routing.decision"
    ]
    assert len(routing_events) == 1
    assert routing_events[0].payload["payload"]["selected_agent"] == "csv_quality"
    assert assistant["metadata"]["stream_fallback"]["reason"] == "dynamic_prompt_stream_unavailable"


def test_prompt_agent_empty_answer_raises_typed_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # #948 S4: the settle/handoff-repair layer that once consumed an empty prompt-
    # agent answer is deleted. An empty answer is a typed failure -- forward raises
    # so the turn records ``agent_error`` (turn.py) instead of a silent empty
    # deliverable, even when the agent declares children.
    import dspy

    from clio_agent.gact import context as ctx
    from clio_agent.gact.app import _build_prompt_user_agent_module

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
        "clio_agent.gact.agents.builders._runtime_dynamic_agent_children_context",
        lambda *args,
        **kwargs: "Declared child experts available for synchronous delegation:\n- mass_spec",
    )

    agent_def = AgentDef(
        id="main",
        source="expert_pack",
        title="Proteomics Root",
    )

    app_token = ctx.set_app(object())
    session_token = ctx.set_session_id("sess_test")
    try:
        module = _build_prompt_user_agent_module(object(), agent_def)
        with pytest.raises(RuntimeError, match="returned an empty answer"):
            module.forward(question="review mzML", session_id="sess_test")
    finally:
        ctx.reset(session_token)
        ctx.reset(app_token)
