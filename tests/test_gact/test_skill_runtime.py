"""Progressive-disclosure skill runtime (#916 S3 / #919).

Tier 1: a react expert that declares ``skills:`` gets a metadata block (names +
descriptions) in its system prompt and the auto-attached ``load_skill`` tool —
the BODY stays out of the prompt until the model loads it (the sabotage twin).
Tool-less predict/CoT experts get the bodies compiled in (their only tier).
The default-registry root expert auto-declares workspace skills.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from clio_agent.gact.agents.builders import _build_blueprint_dspy_module
from clio_agent.gact.agents.skill_runtime import (
    build_load_skill_tool,
    build_spawn_skill_task_tool,
    effective_declared_skills,
    skill_runtime_for_agent,
    skill_runtime_spawns_subagents,
)
from clio_agent.gact.skills import SkillCatalog
from clio_agent.gact.types import AgentDef

BODY = "STEP ONE: inspect. STEP TWO: judge. SECRET_PROCEDURE_MARKER."


@pytest.fixture
def pack(tmp_path: Path) -> Path:
    """A pack root shipping one skill + an expert-visible layout."""

    skill_dir = tmp_path / "pack" / "skills" / "quality-rubric"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: quality-rubric\ndescription: Judge data quality\n---\n\n{BODY}\n",
        encoding="utf-8",
    )
    (skill_dir / "references" / "checklist.md").parent.mkdir(parents=True, exist_ok=True)
    (skill_dir / "references" / "checklist.md").write_text("THE CHECKLIST", encoding="utf-8")
    return tmp_path / "pack"


def _agent(pack: Path, **overrides: Any) -> AgentDef:
    fields: dict[str, Any] = {
        "id": "analyst",
        "source": "expert_pack",
        "title": "Analyst",
        "system_prompt": "Analyze things.",
        "skills": ["quality-rubric"],
        "module": {"kind": "react"},
        "metadata": {"pack_definition_path": str(pack / "AGENT.md")},
    }
    fields.update(overrides)
    return AgentDef(**fields)


def _runtime(pack: Path, agent: AgentDef | None = None) -> Any:
    return skill_runtime_for_agent(None, agent or _agent(pack))


# ---- tier 1: the metadata block -------------------------------------------------


def test_prompt_block_has_metadata_never_body(pack: Path) -> None:
    """THE sabotage twin: names + descriptions in, procedure OUT until loaded."""

    rt = _runtime(pack)
    assert "quality-rubric" in rt.prompt_block
    assert "Judge data quality" in rt.prompt_block
    assert "load_skill" in rt.prompt_block
    assert "SECRET_PROCEDURE_MARKER" not in rt.prompt_block


def test_no_declaration_means_no_block(pack: Path) -> None:
    rt = _runtime(pack, _agent(pack, skills=[]))
    assert rt.prompt_block == ""
    assert rt.bodies_block == ""
    assert rt.resolved == {}


def test_spawn_effect_skill_marks_runtime_for_child_task_collection(pack: Path) -> None:
    skill_md = pack / "skills" / "quality-rubric" / "SKILL.md"
    skill_md.write_text(
        "---\n"
        "name: quality-rubric\n"
        "description: Judge data quality\n"
        "effect: spawn_subagent_with_skill\n"
        "---\n\n"
        f"{BODY}\n",
        encoding="utf-8",
    )

    runtime = _runtime(pack)
    assert skill_runtime_spawns_subagents(runtime) is True
    assert "[child-task: use spawn_skill_task]" in runtime.prompt_block
    assert build_spawn_skill_task_tool(_agent(pack), runtime).name == "spawn_skill_task"


def test_load_skill_never_hides_a_child_spawn(pack: Path) -> None:
    """Loading instructions and launching a child are distinct causal actions."""

    from clio_agent.gact.agents.skill_effects import SkillEffectError

    skill_md = pack / "skills" / "quality-rubric" / "SKILL.md"
    skill_md.write_text(
        "---\n"
        "name: quality-rubric\n"
        "description: Judge data quality\n"
        "effect: spawn_subagent_with_skill\n"
        "---\n\n"
        f"{BODY}\n",
        encoding="utf-8",
    )
    with pytest.raises(SkillEffectError) as exc:
        build_load_skill_tool(_agent(pack), _runtime(pack)).func(skill_id="quality-rubric")
    assert exc.value.reason == "spawn_requires_spawn_skill_task"


def test_unresolved_declaration_omitted_from_block(pack: Path) -> None:
    rt = _runtime(pack, _agent(pack, skills=["quality-rubric", "ghost"]))
    assert "quality-rubric" in rt.prompt_block
    assert "ghost" not in rt.prompt_block
    assert rt.resolutions["ghost"].status == "missing"


# ---- tier 2: the load_skill tool -------------------------------------------------


def test_load_skill_returns_body_and_bundled_listing(pack: Path) -> None:
    rt = _runtime(pack)
    tool = build_load_skill_tool(_agent(pack), rt)
    out = tool.func(skill_id="quality-rubric")
    assert "SECRET_PROCEDURE_MARKER" in out
    assert "references/checklist.md" in out


def test_load_skill_reads_fresh_from_disk(pack: Path) -> None:
    """Load-time freshness: an edit after build is honored (progressive
    disclosure reads reality, not a stale snapshot)."""

    rt = _runtime(pack)
    tool = build_load_skill_tool(_agent(pack), rt)
    skill_md = pack / "skills" / "quality-rubric" / "SKILL.md"
    skill_md.write_text(
        "---\nname: quality-rubric\ndescription: D\n---\n\nUPDATED_PROCEDURE.\n",
        encoding="utf-8",
    )
    assert "UPDATED_PROCEDURE." in tool.func(skill_id="quality-rubric")


def test_load_skill_unknown_id_names_declared(pack: Path) -> None:
    rt = _runtime(pack)
    tool = build_load_skill_tool(_agent(pack), rt)
    with pytest.raises(ValueError) as excinfo:
        tool.func(skill_id="nope")
    assert "quality-rubric" in str(excinfo.value)


def test_load_skill_bundled_file_and_traversal_twin(pack: Path) -> None:
    rt = _runtime(pack)
    tool = build_load_skill_tool(_agent(pack), rt)
    assert tool.func(skill_id="quality-rubric", file="references/checklist.md") == "THE CHECKLIST"
    (pack / "secret.txt").write_text("OUTSIDE", encoding="utf-8")
    with pytest.raises(ValueError) as excinfo:
        tool.func(skill_id="quality-rubric", file="../../secret.txt")
    assert "outside" in str(excinfo.value)


# ---- declared structured_content (P5 wire semantics) ------------------------------


def test_load_skill_declares_typed_structured_content_for_body(pack: Path, monkeypatch) -> None:
    """load_skill gets wait_agent_tasks's OWN treatment: a ``message`` naming what
    loaded + its line count FIRST, then the skill id/scope facts. The BODY stays the
    model-facing return UNCHANGED (asserted separately above)."""

    declared: list[dict] = []
    monkeypatch.setattr(
        "clio_agent.gact.agents.tool_instrumentation.declare_structured_content",
        lambda value: declared.append(dict(value)),
    )
    rt = _runtime(pack)
    tool = build_load_skill_tool(_agent(pack), rt)
    out = tool.func(skill_id="quality-rubric")

    assert "SECRET_PROCEDURE_MARKER" in out  # model-facing body unchanged
    assert len(declared) == 1
    shape = declared[0]
    assert next(iter(shape)) == "message"
    assert shape["message"] == "loaded skill 'quality-rubric' (1 line)"
    assert shape["skill_id"] == "quality-rubric"
    assert shape["scope"] == "pack"
    assert shape["lines"] == 1
    assert "file" not in shape


def test_load_skill_declares_typed_structured_content_for_bundled_file(
    pack: Path, monkeypatch
) -> None:
    declared: list[dict] = []
    monkeypatch.setattr(
        "clio_agent.gact.agents.tool_instrumentation.declare_structured_content",
        lambda value: declared.append(dict(value)),
    )
    rt = _runtime(pack)
    tool = build_load_skill_tool(_agent(pack), rt)
    out = tool.func(skill_id="quality-rubric", file="references/checklist.md")

    assert out == "THE CHECKLIST"  # model-facing content unchanged
    assert len(declared) == 1
    shape = declared[0]
    assert (
        shape["message"]
        == "loaded file 'references/checklist.md' from skill 'quality-rubric' (1 line)"
    )
    assert shape["skill_id"] == "quality-rubric"
    assert shape["file"] == "references/checklist.md"


# ---- tool-less experts get bodies -------------------------------------------------


def test_predict_expert_gets_bodies_block(pack: Path) -> None:
    rt = _runtime(pack, _agent(pack, module={"kind": "predict"}))
    assert "SECRET_PROCEDURE_MARKER" in rt.bodies_block
    assert "quality-rubric" in rt.bodies_block


# ---- default-expert workspace auto-declaration ------------------------------------


def test_default_root_auto_declares_workspace_skills_on_real_runtime_rows(
    tmp_path: Path,
) -> None:
    """THE real-object proof: the rows served by the EXECUTING seam
    (``load_agent_blueprints``, what ``_runtime_active_agent_blueprint_rows``
    loads — the conftest fixture installs the default-registry blueprint) must
    trip the auto-declaration for their root expert and ONLY the root."""

    from clio_agent.gact.agent_blueprints import (
        DEFAULT_AGENT_BLUEPRINT_ID,
        load_agent_blueprints,
    )

    ws = tmp_path / "ws"
    d = ws / ".claude" / "skills" / "user-skill"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        "---\nname: user-skill\ndescription: U\n---\n\nUser procedure.\n", encoding="utf-8"
    )
    catalog = SkillCatalog(home=tmp_path / "no-home", cwd=ws)
    rows = load_agent_blueprints(blueprint_id=DEFAULT_AGENT_BLUEPRINT_ID)
    assert rows, "conftest default-registry blueprint fixture must load"
    roots = [r for r in rows if not r.parent_id]
    assert roots, "fixture blueprint must have a root expert"
    for root in roots:
        assert "user-skill" in effective_declared_skills(root, catalog)
    for child in [r for r in rows if r.parent_id]:
        assert "user-skill" not in effective_declared_skills(child, catalog)
    # A root tagged with the EXECUTING seam's own agent_blueprint_id matches:
    listing_root = AgentDef(
        id="main",
        source="expert_pack",
        title="Main",
        metadata={"agent_blueprint_id": DEFAULT_AGENT_BLUEPRINT_ID},
    )
    # Workspace skills lead the surface; clio's shipped built-in skills are
    # auto-declared after them onto the default-registry root
    # (P1.5 #1067), alphabetically by id. #1211 review R6/D1: ``update-models`` is a
    # DELIBERATE inclusion, not an oversight -- there is no per-skill auto-declare
    # opt-out today, its declaration cost is the same ~100-token metadata-only block
    # every built-in pays (RULE 6), and letting it auto-declare (rather than inventing
    # new opt-out machinery) is what lets plain chat invoke `/update-models` without an
    # edited blueprint, matching the existing ``planning`` precedent exactly.
    assert effective_declared_skills(listing_root, catalog) == [
        "user-skill",
        "planning",
        "present-interactive-analysis",
        "update-models",
    ]
    # DELETED SEAM regression pin: the retired "listing seam" stamp
    # (metadata["source_blueprint"] == "default_registry") -- the tag
    # catalog._builtin_agents() used to attach when it implicitly loaded the
    # installed-but-unactivated default registry snapshot -- must NEVER
    # auto-declare on its own; only the executing seam's agent_blueprint_id does.
    stale_listing_tag_root = AgentDef(
        id="main",
        source="expert_pack",
        title="Main",
        metadata={"source_blueprint": "default_registry"},
    )
    assert effective_declared_skills(stale_listing_tag_root, catalog) == []
    # A non-default blueprint root does NOT auto-declare:
    other_root = AgentDef(
        id="root",
        source="expert_pack",
        title="R",
        metadata={"agent_blueprint_id": "some-other-pack"},
    )
    assert effective_declared_skills(other_root, catalog) == []


def test_flat_skill_has_no_bundled_files(scratch_flat: None, tmp_path: Path) -> None:
    """A flat .md skill has no per-skill directory: file= is a typed error and
    the listing never exposes sibling skills' bundles."""

    root = tmp_path / "ws" / ".claude" / "skills"
    root.mkdir(parents=True)
    (root / "flat.md").write_text(
        "---\nname: flat\ndescription: F\n---\n\nFLAT BODY.\n", encoding="utf-8"
    )
    (root / "sibling-secret.txt").write_text("SIBLING", encoding="utf-8")
    agent = AgentDef(id="a", source="expert_pack", title="A", skills=["flat"], metadata={})
    catalog = SkillCatalog(home=tmp_path / "no-home", cwd=tmp_path / "ws")
    from clio_agent.gact.agents.skill_runtime import SkillRuntime

    rt = SkillRuntime(resolutions=catalog.resolve_declared(["flat"]))
    tool = build_load_skill_tool(agent, rt)
    out = tool.func(skill_id="flat")
    assert "FLAT BODY." in out
    assert "sibling-secret" not in out
    with pytest.raises(ValueError) as excinfo:
        tool.func(skill_id="flat", file="sibling-secret.txt")
    assert "flat .md skill" in str(excinfo.value)


@pytest.fixture
def scratch_flat() -> None:
    return None


def test_bundled_listing_skips_dotfiles(pack: Path) -> None:
    hidden = pack / "skills" / "quality-rubric" / ".git" / "config"
    hidden.parent.mkdir(parents=True)
    hidden.write_text("secret", encoding="utf-8")
    rt = _runtime(pack)
    out = build_load_skill_tool(_agent(pack), rt).func(skill_id="quality-rubric")
    assert ".git" not in out
    assert "references/checklist.md" in out


def test_appless_rebuild_serves_cached_surface(pack: Path, tmp_path: Path) -> None:
    """Prompt-prefix stability across build paths: the app-less sync rebuild
    reuses the surface computed on the context-bearing build of the same app."""

    from clio_agent.gact import context as _ctx
    from clio_agent.gact.app import build_app

    app = build_app(sessions_path=tmp_path / "s.json")
    agent = _agent(pack)
    rt1 = skill_runtime_for_agent(app, agent)
    assert rt1.resolved
    token = _ctx.set_app(app)
    try:
        rt2 = skill_runtime_for_agent(None, agent)
    finally:
        _ctx.reset(token)
    assert rt2 is rt1


# ---- builder integration ------------------------------------------------------------


def test_react_builder_wires_block_and_tool(pack: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """End-to-end through the REAL builder: system prompt carries the metadata
    block (not the body) and the tools include load_skill."""

    monkeypatch.setattr("clio_agent.config.create_lm", lambda config: object(), raising=True)
    monkeypatch.setattr(
        "clio_agent.config.create_chat_adapter", lambda config: object(), raising=True
    )
    monkeypatch.setattr(
        "clio_agent.gact.agents.builders._dynamic_agent_lm_config",
        lambda base_agent, agent_def: SimpleNamespace(
            materialize=lambda cred=None: SimpleNamespace(
                provider="openai", model="m", temperature=0.0
            )
        ),
        raising=True,
    )
    module = _build_blueprint_dspy_module(SimpleNamespace(tool_executor=None), _agent(pack))
    assert "quality-rubric" in module.system_prompt
    assert "SECRET_PROCEDURE_MARKER" not in module.system_prompt
    assert any(getattr(t, "name", "") == "load_skill" for t in module.tools)


def test_tool_user_agent_builder_wires_block_and_tool(
    pack: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The third module class (tool-declaring user agents) gets the same react
    tier-1 + load_skill contract — no silent skill drop."""

    from clio_agent.gact.agents.builders import _build_tool_user_agent_module

    monkeypatch.setattr("clio_agent.config.create_lm", lambda config: object(), raising=True)
    monkeypatch.setattr(
        "clio_agent.config.create_chat_adapter", lambda config: object(), raising=True
    )
    monkeypatch.setattr(
        "clio_agent.gact.agents.builders._dynamic_agent_lm_config",
        lambda base_agent, agent_def: SimpleNamespace(
            materialize=lambda cred=None: SimpleNamespace(
                provider="openai", model="m", temperature=0.0
            )
        ),
        raising=True,
    )
    module = _build_tool_user_agent_module(
        SimpleNamespace(tool_executor=None),
        _agent(pack, source="user", module={}),
    )
    assert "quality-rubric" in module.system_prompt
    assert "SECRET_PROCEDURE_MARKER" not in module.system_prompt
    assert any(getattr(t, "name", "") == "load_skill" for t in module.tools)


def test_predict_builder_wires_bodies(pack: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("clio_agent.config.create_lm", lambda config: object(), raising=True)
    monkeypatch.setattr(
        "clio_agent.config.create_chat_adapter", lambda config: object(), raising=True
    )
    monkeypatch.setattr(
        "clio_agent.gact.agents.builders._dynamic_agent_lm_config",
        lambda base_agent, agent_def: SimpleNamespace(
            materialize=lambda cred=None: SimpleNamespace(
                provider="openai", model="m", temperature=0.0
            )
        ),
        raising=True,
    )
    module = _build_blueprint_dspy_module(
        SimpleNamespace(tool_executor=None), _agent(pack, module={"kind": "predict"})
    )
    assert "SECRET_PROCEDURE_MARKER" in module.system_prompt
    assert module.tools == []
