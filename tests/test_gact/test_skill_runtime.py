"""Progressive-disclosure skill runtime (#916 S3 / #919).

Tier 1: an agent whose resolved plugins ship ``skills/`` gets a metadata block
(names + descriptions) in its system prompt and the ``load_skill`` tool — the BODY
stays out of the prompt until the model loads it (the sabotage twin). Tool-less
agents get the bodies compiled in (their only tier). Skills come only from the
resolved plugins (plus the session's A2UI catalog skills for a surface producer);
nothing is auto-declared from the workspace or a built-in folder.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from clio_agent.gact.agents.builders import build_agent_module
from clio_agent.gact.agents.resolution import resolution_agent_def, session_agent_def
from clio_agent.gact.agents.skill_runtime import (
    build_load_skill_tool,
    build_spawn_skill_task_tool,
    effective_declared_skills,
    skill_runtime_for_agent,
    skill_runtime_spawns_subagents,
)
from clio_agent.gact.app import build_app
from clio_agent.gact.skills import SkillCatalog
from clio_agent.gact.types import AgentDef
from clio_agent.plugins.service import PluginService
from tests._plugin_fixtures import FIXTURE, custom_marketplace

from .a2ui_catalog_binding import MINIMAL_PLUGIN, bind_builtin_catalogs, bind_session_plugins

pytestmark = pytest.mark.usefixtures("clio_core_plane")

BODY = "STEP ONE: inspect. STEP TWO: judge. SECRET_PROCEDURE_MARKER."


@pytest.fixture(autouse=True)
def _drop_declared_structured_content() -> Any:
    """``load_skill`` called directly (outside the instrumented wrapper) declares its
    wire payload in this context; pop it so it never leaks into a later test."""

    from clio_agent.gact.agents.tool_instrumentation import pop_declared_structured_content

    yield
    pop_declared_structured_content()


@pytest.fixture
def pack(tmp_path: Path) -> Path:
    """A plugin root shipping one skill with a bundled reference file."""

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
        "source": "plugin",
        "title": "Analyst",
        "system_prompt": "Analyze things.",
        "skills": ["quality-rubric"],
        "module": {"kind": "react"},
        "metadata": {"skill_folders": [("quality", str(pack / "skills"))]},
    }
    fields.update(overrides)
    return AgentDef(**fields)


def _runtime(pack: Path, agent: AgentDef | None = None) -> Any:
    return skill_runtime_for_agent(None, agent or _agent(pack))


def test_load_records_exact_procedure_and_bundled_body_at_load_time(
    pack: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import hashlib

    from clio_agent.gact.runtime import globals as runtime_globals

    recorded: list[dict[str, Any]] = []

    def capture(*args: Any, **kwargs: Any) -> None:
        recorded.append(kwargs["payload"])

    monkeypatch.setattr(runtime_globals, "_emit_semantic_event", capture)
    tool = build_load_skill_tool(_agent(pack), _runtime(pack))
    tool.func(skill_id="quality-rubric")
    tool.func(skill_id="quality-rubric", file="references/checklist.md")
    assert BODY in recorded[0]["content"]
    assert recorded[1]["content"] == "THE CHECKLIST"
    for row in recorded:
        assert row["content_sha256"] == hashlib.sha256(row["content"].encode()).hexdigest()
        assert row["size"] == len(row["content"].encode())
        assert row["skill_description"] == "Judge data quality"
        assert Path(row["skill_directory"]).samefile(pack / "skills/quality-rubric")
    (pack / "skills/quality-rubric/references/checklist.md").write_text(
        "later edit", encoding="utf-8"
    )
    assert recorded[1]["content"] == "THE CHECKLIST"


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
    assert f"Skill directory (use as SKILL_ROOT): {pack / 'skills' / 'quality-rubric'}" in out
    assert "references/checklist.md" in out


def test_load_skill_hides_private_environments_and_bytecode(pack: Path) -> None:
    skill = pack / "skills" / "quality-rubric"
    (skill / ".venv" / "Lib").mkdir(parents=True)
    (skill / ".venv" / "Lib" / "private.py").write_text("private", encoding="utf-8")
    (skill / "scripts" / "__pycache__").mkdir(parents=True)
    (skill / "scripts" / "__pycache__" / "helper.pyc").write_bytes(b"bytecode")

    out = build_load_skill_tool(_agent(pack), _runtime(pack)).func(skill_id="quality-rubric")

    assert ".venv" not in out
    assert "__pycache__" not in out
    assert ".pyc" not in out


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
    loaded + its line count FIRST, then the skill id/scope facts. The model-facing
    return also names the resolved skill directory for executable helpers."""

    declared: list[dict] = []
    monkeypatch.setattr(
        "clio_agent.gact.agents.tool_instrumentation.declare_structured_content",
        lambda value: declared.append(dict(value)),
    )
    rt = _runtime(pack)
    tool = build_load_skill_tool(_agent(pack), rt)
    out = tool.func(skill_id="quality-rubric")

    assert "SECRET_PROCEDURE_MARKER" in out
    assert "Skill directory (use as SKILL_ROOT):" in out
    assert len(declared) == 1
    shape = declared[0]
    assert next(iter(shape)) == "message"
    assert shape["message"] == "loaded skill 'quality-rubric' (1 line)"
    assert shape["skill_id"] == "quality-rubric"
    assert shape["scope"] == "plugin"
    assert shape["lines"] == 1
    assert shape["skill_description"] == "Judge data quality"
    assert Path(shape["skill_directory"]).samefile(pack / "skills/quality-rubric")
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
    assert Path(shape["file_paths"]["references/checklist.md"]).samefile(
        pack / "skills/quality-rubric/references/checklist.md"
    )


def test_reference_paths_identify_the_actual_extra_root_for_batched_fragments(
    pack: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    declared: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "clio_agent.gact.agents.tool_instrumentation.declare_structured_content",
        lambda value: declared.append(dict(value)),
    )
    extra = pack / "shared-catalog"
    extra.mkdir()
    (extra / "catalog.json").write_text(
        json.dumps({"components": {"Chart": {"type": "object"}, "Map": {"type": "object"}}}),
        encoding="utf-8",
    )
    runtime = _runtime(pack)
    resolution = runtime.resolutions["quality-rubric"]
    assert resolution.skill is not None
    runtime.resolutions["quality-rubric"] = replace(
        resolution, skill=replace(resolution.skill, extra_dirs=(str(extra),))
    )
    tool = build_load_skill_tool(_agent(pack), runtime)
    files = ["catalog.json#/components/Chart", "catalog.json#/components/Map"]
    body = tool.func(skill_id="quality-rubric", files=files)
    assert "File 1/2" in body and "File 2/2" in body
    assert set(declared[0]["file_paths"]) == set(files)
    assert all(
        Path(path).samefile(extra / "catalog.json") for path in declared[0]["file_paths"].values()
    )


# ---- tool-less experts get bodies -------------------------------------------------


def test_predict_expert_gets_bodies_block(pack: Path) -> None:
    rt = _runtime(pack, _agent(pack, module={"kind": "predict"}))
    assert "SECRET_PROCEDURE_MARKER" in rt.bodies_block
    assert "quality-rubric" in rt.bodies_block


# ---- skills come only from resolved plugins --------------------------------------

ANALYST_AGENT = """
from clio_agent.sdk import Agent


class Analyst(Agent):
    id = "analyst"
    version = "1.0.0"
    name = "Analyst"
    role = "a data analyst"
    plugins = Agent.plugins + ["quality"]
"""


#: The skills every agent carries: core's planning and the fleet plugin's update-models.
CORE_SKILLS = ("planning", "update-models")


def _write_inventory_skill(root: Path, skill_id: str, description: str, body: str) -> Path:
    """Write a real installed procedure for inventory and precedence checks."""
    path = root / skill_id / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\nname: {skill_id}\ndescription: {description}\n---\n\n{body}\n",
        encoding="utf-8",
    )
    return path


def _quality_marketplace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A default marketplace whose ``quality`` plugin ships two skills, one with a script."""

    skill = "---\nname: {id}\ndescription: {description}\n---\n\n{body}\n"
    return custom_marketplace(
        tmp_path,
        monkeypatch,
        plugins={
            "quality": {
                "files": {
                    "skills/quality-rubric/SKILL.md": skill.format(
                        id="quality-rubric", description="Judge data quality", body=BODY
                    ),
                    "skills/quality-rubric/scripts/check.py": "print('checked')\n",
                    "skills/new-format/SKILL.md": skill.format(
                        id="new-format",
                        description="Author the new format",
                        body="NEW FORMAT BODY",
                    ),
                }
            }
        },
        agents={"analyst.py": ANALYST_AGENT},
    )


def test_resolved_plugin_skills_are_the_agents_skills_metadata_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every skill a resolved plugin ships is discoverable, metadata-only, then loadable."""

    _quality_marketplace(tmp_path, monkeypatch)
    agent = resolution_agent_def(PluginService().resolve("clio/analyst", pin=False))
    # The analyst's plugins are core (planning, update-models) and quality.
    assert sorted(agent.skills) == sorted([*CORE_SKILLS, "new-format", "quality-rubric"])
    runtime = skill_runtime_for_agent(None, agent)
    rubric = runtime.resolved["quality-rubric"].skill
    assert rubric is not None and rubric.scope == "plugin" and rubric.source == "quality"
    assert "- new-format: Author the new format" in runtime.prompt_block
    assert "Call load_skill" in runtime.prompt_block
    assert "NEW FORMAT BODY" not in runtime.prompt_block
    assert BODY not in runtime.prompt_block
    tool = build_load_skill_tool(agent, runtime)
    assert "NEW FORMAT BODY" in tool.func(skill_id="new-format")
    loaded = tool.func(skill_id="quality-rubric")
    assert "SECRET_PROCEDURE_MARKER" in loaded
    assert "scripts/check.py" in loaded
    assert (Path(rubric.dir) / "scripts" / "check.py").is_file()


def test_workspace_and_global_skills_are_never_auto_declared(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A plugin agent declares only its plugins' skills; user skill folders stay private."""

    _quality_marketplace(tmp_path, monkeypatch)
    workspace, home = tmp_path / "workspace", tmp_path / "home"
    _write_inventory_skill(workspace / ".claude" / "skills", "user-skill", "U", "USER BODY")
    _write_inventory_skill(home / ".codex" / "skills", "private-global", "P", "PRIVATE BODY")
    catalog = SkillCatalog(home=home, cwd=workspace)
    root = resolution_agent_def(PluginService().resolve("clio/analyst", pin=False))
    assert sorted(effective_declared_skills(root, catalog)) == sorted(
        [*CORE_SKILLS, "new-format", "quality-rubric"]
    )
    standard = resolution_agent_def(PluginService().resolve("clio/standard", pin=False))
    assert sorted(effective_declared_skills(standard, catalog)) == sorted(CORE_SKILLS)
    child = root.model_copy(update={"id": "child", "parent_id": "analyst", "skills": []})
    assert effective_declared_skills(child, catalog) == []


def test_plugin_skill_shadows_a_workspace_skill_of_the_same_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Precedence: the agent's resolved plugin wins over the user's workspace copy."""

    _quality_marketplace(tmp_path, monkeypatch)
    workspace = tmp_path / "workspace"
    _write_inventory_skill(
        workspace / ".agents" / "skills", "quality-rubric", "Local procedure", "LOCAL BODY"
    )
    agent = resolution_agent_def(PluginService().resolve("clio/analyst", pin=False))
    catalog = SkillCatalog(
        home=tmp_path / "home",
        cwd=workspace,
        plugin_skills=agent.metadata["skill_folders"],
    )
    resolution = catalog.resolve("quality-rubric")
    assert resolution.status == "resolved"
    assert resolution.skill is not None and resolution.skill.scope == "plugin"
    from clio_agent.gact.agents.skill_runtime import SkillRuntime

    runtime = SkillRuntime(resolutions=catalog.resolve_declared(["quality-rubric"]))
    loaded = build_load_skill_tool(agent, runtime).func(skill_id="quality-rubric")
    assert "SECRET_PROCEDURE_MARKER" in loaded
    assert "LOCAL BODY" not in loaded


@pytest.mark.marketplace
def test_interactive_analysis_skill_is_when_why_guidance_with_no_prop_lore() -> None:
    """The presentation skill points to catalog skills without duplicating prop lore."""

    from tests._marketplace import MARKETPLACE_ROOT

    skill_path = (
        MARKETPLACE_ROOT
        / "plugins"
        / "visuals"
        / "skills"
        / "present-interactive-analysis"
        / "SKILL.md"
    )
    body = skill_path.read_text(encoding="utf-8")

    # Preserves the "a human choice must be delivered to the agent" guidance...
    assert "local visual state only" in body
    assert "submit action" in body
    # ...but never as a concrete component/prop recipe (deleted, S4).
    assert "```yaml" not in body
    assert "component: ChoicePicker" not in body
    assert "selectedStationIds" not in body
    # Points at catalog skills as the source of truth for exact shapes.
    assert 'load_skill("a2ui-catalog-<slug>")' in body
    assert 'file="catalog.json#/components/<ExactComponentId>")' in body
    assert "generated images and SVG figures" in body
    assert "Give a new figure a distinct surface ID" in body
    assert "Show each figure once in A2UI" in body
    # #1533 S4 adversarial review item 7: the retired clio.time-series.v1
    # component is never named as a distinct view choice -- both time-series
    # and multi-entity bullets point at the one real chart component.
    assert "an interactive time series" not in " ".join(body.split())
    assert "clio.chart.v1" in body
    assert "clio.time-series.v1" not in body


def test_flat_skill_has_no_bundled_files(scratch_flat: None, tmp_path: Path) -> None:
    """A flat .md skill has no per-skill directory: file= is a typed error and
    the listing never exposes sibling skills' bundles."""

    root = tmp_path / "ws" / ".claude" / "skills"
    root.mkdir(parents=True)
    (root / "flat.md").write_text(
        "---\nname: flat\ndescription: F\n---\n\nFLAT BODY.\n", encoding="utf-8"
    )
    (root / "sibling-secret.txt").write_text("SIBLING", encoding="utf-8")
    agent = AgentDef(id="a", source="plugin", title="A", skills=["flat"], metadata={})
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


def _patch_agent_lm(monkeypatch: pytest.MonkeyPatch) -> None:
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


def test_react_builder_wires_block_and_tool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end through the REAL builder: system prompt carries the metadata
    block (not the body) and the tools include load_skill."""

    _quality_marketplace(tmp_path, monkeypatch)
    _patch_agent_lm(monkeypatch)
    resolution = PluginService().resolve("clio/analyst", pin=False)
    assert "load_skill" in {row["name"] for row in resolution.tools}
    module = build_agent_module(
        SimpleNamespace(tool_executor=None), resolution_agent_def(resolution), resolution
    )
    assert module.system_prompt.startswith(resolution.prompt_text.strip())
    assert "quality-rubric" in module.system_prompt
    assert "SECRET_PROCEDURE_MARKER" not in module.system_prompt
    assert any(getattr(t, "name", "") == "load_skill" for t in module.tools)


def test_builder_attaches_no_load_skill_to_an_agent_without_skills(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An agent whose plugins ship no skills gets no skill block and no load_skill."""

    _quality_marketplace(tmp_path, monkeypatch)
    _patch_agent_lm(monkeypatch)
    resolution = PluginService().resolve("clio/analyst", pin=False)
    agent = resolution_agent_def(resolution).model_copy(
        update={"skills": [], "metadata": {"skill_folders": []}}
    )
    module = build_agent_module(SimpleNamespace(tool_executor=None), agent, resolution)
    assert "## Skills available to you" not in module.system_prompt
    assert not any(getattr(t, "name", "") == "load_skill" for t in module.tools)


def test_predict_builder_wires_bodies(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _quality_marketplace(tmp_path, monkeypatch)
    _patch_agent_lm(monkeypatch)
    resolution = PluginService().resolve("clio/analyst", pin=False)
    agent = resolution_agent_def(resolution).model_copy(update={"module": {"kind": "predict"}})
    module = build_agent_module(SimpleNamespace(tool_executor=None), agent, resolution)
    assert "SECRET_PROCEDURE_MARKER" in module.system_prompt
    assert module.tools == []


# ---- S4: load_skill JSON pointer fragment support --------------------------------


def test_load_skill_file_fragment_resolves_a_json_pointer(pack: Path) -> None:
    catalog = {
        "components": {
            "Button": {
                "type": "object",
                "properties": {
                    "action": {"$ref": "common_types.json#/$defs/Action"},
                    "component": {"const": "Button"},
                },
            }
        }
    }
    (pack / "skills" / "quality-rubric" / "catalog.json").write_text(
        json.dumps(catalog), encoding="utf-8"
    )
    rt = _runtime(pack)
    tool = build_load_skill_tool(_agent(pack), rt)

    out = tool.func(skill_id="quality-rubric", file="catalog.json#/components/Button")

    assert '"const": "Button"' in out
    assert "common_types.json#/$defs/Action" in out


def test_load_skill_file_fragment_unresolvable_lists_available_keys(pack: Path) -> None:
    catalog = {"components": {"Button": {"type": "object"}}}
    (pack / "skills" / "quality-rubric" / "catalog.json").write_text(
        json.dumps(catalog), encoding="utf-8"
    )
    rt = _runtime(pack)
    tool = build_load_skill_tool(_agent(pack), rt)

    with pytest.raises(ValueError) as excinfo:
        tool.func(skill_id="quality-rubric", file="catalog.json#/components/Missing")

    assert "Button" in str(excinfo.value)
    assert "does not resolve" in str(excinfo.value)


def test_load_skill_file_fragment_requires_json(pack: Path) -> None:
    rt = _runtime(pack)
    tool = build_load_skill_tool(_agent(pack), rt)

    with pytest.raises(ValueError) as excinfo:
        tool.func(skill_id="quality-rubric", file="references/checklist.md#/x")

    assert "fragment" in str(excinfo.value)


def test_load_skill_file_fragment_requires_absolute_pointer(pack: Path) -> None:
    (pack / "skills" / "quality-rubric" / "catalog.json").write_text("{}", encoding="utf-8")
    rt = _runtime(pack)
    tool = build_load_skill_tool(_agent(pack), rt)

    with pytest.raises(ValueError) as excinfo:
        tool.func(skill_id="quality-rubric", file="catalog.json#components")

    assert "absolute" in str(excinfo.value)


def test_load_skill_file_without_fragment_is_unaffected(pack: Path) -> None:
    """No ``#`` in ``file`` is the pre-existing, unchanged path."""

    rt = _runtime(pack)
    tool = build_load_skill_tool(_agent(pack), rt)

    assert tool.func(skill_id="quality-rubric", file="references/checklist.md") == "THE CHECKLIST"


# ---- #1533 phase 3: multi-file load_skill (files=[...]) ---------------------------


def test_load_skill_files_returns_several_bundled_files_in_one_labelled_call(
    pack: Path,
) -> None:
    catalog = {
        "components": {
            "Button": {"type": "object", "properties": {"component": {"const": "Button"}}},
            "Text": {"type": "object", "properties": {"component": {"const": "Text"}}},
        }
    }
    (pack / "skills" / "quality-rubric" / "catalog.json").write_text(
        json.dumps(catalog), encoding="utf-8"
    )
    rt = _runtime(pack)
    tool = build_load_skill_tool(_agent(pack), rt)

    out = tool.func(
        skill_id="quality-rubric",
        files=[
            "catalog.json#/components/Button",
            "catalog.json#/components/Text",
            "references/checklist.md",
        ],
    )

    assert "=== File 1/3: catalog.json#/components/Button ===" in out
    assert "=== File 2/3: catalog.json#/components/Text ===" in out
    assert "=== File 3/3: references/checklist.md ===" in out
    assert '"const": "Button"' in out
    assert '"const": "Text"' in out
    assert "THE CHECKLIST" in out
    # Order in the output matches the order requested.
    assert out.index("Button") < out.index("Text") < out.index("THE CHECKLIST")


def test_load_skill_files_with_one_entry_matches_the_bare_file_form(pack: Path) -> None:
    """A single-element ``files=[...]`` behaves exactly like ``file=`` (no headers)."""

    rt = _runtime(pack)
    tool = build_load_skill_tool(_agent(pack), rt)

    assert (
        tool.func(skill_id="quality-rubric", files=["references/checklist.md"]) == "THE CHECKLIST"
    )


def test_load_skill_combines_file_and_files_without_duplicate_reads(pack: Path) -> None:
    (pack / "skills" / "quality-rubric" / "references" / "second.md").write_text(
        "SECOND CHECKLIST", encoding="utf-8"
    )
    rt = _runtime(pack)
    tool = build_load_skill_tool(_agent(pack), rt)

    out = tool.func(
        skill_id="quality-rubric",
        file="references/checklist.md",
        files=["references/checklist.md", "references/second.md"],
    )
    assert out.count("THE CHECKLIST") == 1
    assert "SECOND CHECKLIST" in out
    assert "1 duplicate file request" in out


def test_load_skill_files_propagates_an_unresolvable_entry(pack: Path) -> None:
    rt = _runtime(pack)
    tool = build_load_skill_tool(_agent(pack), rt)

    with pytest.raises(ValueError) as excinfo:
        tool.func(skill_id="quality-rubric", files=["references/checklist.md", "missing.md"])

    assert "unreadable" in str(excinfo.value) or "outside" in str(excinfo.value)


# ---- S4: catalogs disclosed as skill directories ----------------------------------


def test_default_session_agent_declares_the_core_catalog_skill(tmp_path: Path) -> None:
    """A bare session runs clio/standard, whose core plugin declares clio-workspace."""

    app = build_app(sessions_path=tmp_path / "sessions.json")
    session = app.state.sessions.create(workspace_id="ws_default", title="root")
    root = session_agent_def(app, session.id)
    assert "create_a2ui_surface" in root.tools

    rt = skill_runtime_for_agent(app, root, session_id=session.id)

    catalog_skills = [skill_id for skill_id in rt.resolved if skill_id.startswith("a2ui-")]
    assert catalog_skills == ["a2ui-catalog-clio-workspace"]


def test_session_agent_sees_plugin_skills_and_catalog_skills_together(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A chat can discover when to offer editable views (a plugin skill), not only
    their schema (the generated catalog skill)."""

    visuals = FIXTURE / "plugins" / "visuals" / "ai.iowarp.clio" / "tools" / "builtin.py"
    custom_marketplace(
        tmp_path,
        monkeypatch,
        plugins={
            "visuals": {
                "files": {
                    "ai.iowarp.clio/tools/builtin.py": visuals.read_text(encoding="utf-8"),
                    "skills/present-interactive-analysis/SKILL.md": (
                        "---\nname: present-interactive-analysis\n"
                        "description: Offer editable message drafts and A2UI figures\n---\n\n"
                        "PRESENTATION BODY\n"
                    ),
                }
            }
        },
    )
    app = build_app(sessions_path=tmp_path / "sessions.json")
    session = app.state.sessions.create(workspace_id="ws_default", title="draft")
    runtime = skill_runtime_for_agent(
        app, session_agent_def(app, session.id), session_id=session.id
    )

    # Plugin skills follow resolution order: core, then the agent's plugins, then
    # the plugins they require (fleet, which core requires, resolves after visuals).
    assert list(runtime.resolved) == [
        "planning",
        "present-interactive-analysis",
        "update-models",
        "a2ui-catalog-clio-workspace",
    ]
    assert runtime.resolved["present-interactive-analysis"].skill.scope == "plugin"
    assert "editable message drafts" in runtime.prompt_block
    assert "PRESENTATION BODY" not in runtime.prompt_block


def test_root_agent_declares_one_catalog_skill_line_per_declared_catalog_in_order(
    tmp_path: Path,
) -> None:
    """Catalog skills follow the session's declared catalog order."""

    from clio_agent.gact.a2ui_catalogs.builtin import load_builtin_catalogs

    app = build_app(sessions_path=tmp_path / "sessions.json")
    session = app.state.sessions.create(workspace_id="ws_default", title="root")
    bind_builtin_catalogs(app, session.id)  # declares [clio-workspace, basic]
    root = session_agent_def(app, session.id)

    rt = skill_runtime_for_agent(app, root, session_id=session.id)

    basic, workspace = load_builtin_catalogs()
    assert list(rt.resolved) == [*CORE_SKILLS, "a2ui-catalog-clio-workspace", "a2ui-catalog-basic"]
    lines = rt.prompt_block.splitlines()
    assert lines[0] == "## Skills available to you"
    assert [line for line in lines[2:] if line.startswith("- a2ui-catalog-")] == [
        f"- a2ui-catalog-clio-workspace: {workspace.file['description']}",
        f"- a2ui-catalog-basic: {basic.file['description']}",
    ]


def test_catalog_skill_body_generator_runs_once_across_twenty_turns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Perf fix (CI investigation, PR #1375): skill_runtime_for_agent's
    tier-2 pass (skill_bodies_context) reads EVERY resolved skill's body on
    every call, even for a react expert that only ever uses prompt_block --
    without the checksum-keyed cache in a2ui_catalogs/skills.py, a session
    with two auto-declared catalog skills regenerated both bodies on every
    single turn build. Twenty builds must render each catalog's body ONCE."""

    from clio_agent.gact.a2ui_catalogs import skills as cat_skills_mod

    # The cache is process-global, keyed by content checksum (mirrors
    # registry.py's compiled_validators/_VALIDATOR_CACHE) -- an earlier test
    # in the same session may have already warmed it for these same builtin
    # catalogs. Start this test from a known-cold state for a deterministic
    # count instead of asserting on residual state from test order.
    cat_skills_mod._BODY_CACHE.clear()

    render_calls: list[str] = []
    original = cat_skills_mod._render_catalog_skill_body

    def counting(entry: Any, **kwargs: Any) -> str:
        render_calls.append(entry.checksum)
        return original(entry, **kwargs)

    monkeypatch.setattr(cat_skills_mod, "_render_catalog_skill_body", counting)

    app = build_app(sessions_path=tmp_path / "sessions.json")
    session = app.state.sessions.create(workspace_id="ws_default", title="root")
    bind_builtin_catalogs(app, session.id)
    root = session_agent_def(app, session.id)

    for _ in range(20):
        skill_runtime_for_agent(app, root, session_id=session.id)

    # Two producible builtin catalogs (basic, clio-workspace) -- one render
    # call per DISTINCT checksum across all twenty builds, never twenty.
    assert len(render_calls) == len(set(render_calls)) == 2


def test_plugin_shipped_catalog_is_disclosed_as_a_skill_after_core(tmp_path: Path) -> None:
    """A catalog a plugin ships becomes a catalog skill, after core's clio-workspace."""

    app = build_app(sessions_path=tmp_path / "sessions.json")
    session = app.state.sessions.create(workspace_id="ws_default", title="root")
    bind_session_plugins(app, session.id, MINIMAL_PLUGIN)
    root = session_agent_def(app, session.id)

    rt = skill_runtime_for_agent(app, root, session_id=session.id)

    assert list(rt.resolved) == [
        *CORE_SKILLS,
        "a2ui-catalog-clio-workspace",
        "a2ui-catalog-minimal",
    ]


def test_producer_tool_declaration_auto_declares_catalog_skills_for_a_child(
    tmp_path: Path,
) -> None:
    """A non-root expert gets catalog skills too, purely from declaring a producer tool."""

    app = build_app(sessions_path=tmp_path / "sessions.json")
    session = app.state.sessions.create(workspace_id="ws_default", title="child")
    bind_builtin_catalogs(app, session.id)
    child = AgentDef(
        id="visual",
        title="Visual",
        parent_id="root",
        module={"kind": "react"},
        tools=["create_a2ui_surface"],
    )

    rt = skill_runtime_for_agent(app, child, session_id=session.id)

    assert "a2ui-catalog-basic" in rt.resolved
    assert "a2ui-catalog-clio-workspace" in rt.resolved


def test_catalog_scope_records_a_typed_scan_error_when_session_scoped_but_no_registry(
    tmp_path: Path,
) -> None:
    """Adversarial-review fix: an app IS present (a real request/session
    context) but no session id / no a2ui_catalogs registry -- this is a
    degradation, not the app-less unit "nothing to disclose" case, and must
    be recorded to scan_errors instead of returning [] silently."""

    from types import SimpleNamespace

    stub_app = SimpleNamespace(state=SimpleNamespace())  # no a2ui_catalogs attribute
    catalog = SkillCatalog(app=stub_app, session_id="sess_x")

    refs = catalog._catalog_refs()

    assert refs == []
    assert catalog.scan_errors == [
        {"scope": "catalog", "error": "a2ui_catalog_registry_unavailable"}
    ]


def test_catalog_scope_app_less_unit_case_stays_silent(tmp_path: Path) -> None:
    """The documented ONE silent case: no app at all (a bare SkillCatalog(),
    as most of this test file's own fixtures build) is not a degradation --
    there is no session to scope producibility to in the first place."""

    catalog = SkillCatalog(cwd=tmp_path)

    refs = catalog._catalog_refs()

    assert refs == []
    assert catalog.scan_errors == []


def test_plain_child_with_no_producer_tool_gets_no_catalog_skills(tmp_path: Path) -> None:
    app = build_app(sessions_path=tmp_path / "sessions.json")
    session = app.state.sessions.create(workspace_id="ws_default", title="child")
    child = AgentDef(id="plain", title="Plain", parent_id="root", module={"kind": "react"})

    rt = skill_runtime_for_agent(app, child, session_id=session.id)

    assert rt.resolved == {}


def test_load_skill_on_an_undeclared_catalog_skill_is_the_existing_not_declared_error(
    tmp_path: Path,
) -> None:
    app = build_app(sessions_path=tmp_path / "sessions.json")
    session = app.state.sessions.create(workspace_id="ws_default", title="child")
    child = AgentDef(id="plain", title="Plain", parent_id="root", module={"kind": "react"})
    rt = skill_runtime_for_agent(app, child, session_id=session.id)
    tool = build_load_skill_tool(child, rt)

    with pytest.raises(ValueError) as excinfo:
        tool.func(skill_id="a2ui-catalog-basic")

    assert "unknown skill" in str(excinfo.value)


def test_catalog_skill_body_carries_instructions_index_and_load_skill_call(
    tmp_path: Path,
) -> None:
    from clio_agent.gact.a2ui_catalogs.builtin import load_builtin_catalogs

    app = build_app(sessions_path=tmp_path / "sessions.json")
    session = app.state.sessions.create(workspace_id="ws_default", title="root")
    root = session_agent_def(app, session.id)
    rt = skill_runtime_for_agent(app, root, session_id=session.id)
    tool = build_load_skill_tool(root, rt)

    body = tool.func(skill_id="a2ui-catalog-clio-workspace")

    _, workspace = load_builtin_catalogs()
    assert workspace.instructions.strip() in body
    assert "## Components" in body
    assert "`clio.status.v1`" in body
    assert (
        'load_skill("a2ui-catalog-clio-workspace", file="catalog.json#/components/<Name>")' in body
    )


def test_catalog_skill_component_file_matches_the_validated_catalog(tmp_path: Path) -> None:
    """The loaded component schema is read from the SAME file the server validates
    against (S2's allowlist), never a second maintained copy."""

    app = build_app(sessions_path=tmp_path / "sessions.json")
    session = app.state.sessions.create(workspace_id="ws_default", title="root")
    bind_builtin_catalogs(app, session.id)
    root = session_agent_def(app, session.id)
    rt = skill_runtime_for_agent(app, root, session_id=session.id)
    tool = build_load_skill_tool(root, rt)

    out = tool.func(skill_id="a2ui-catalog-basic", file="catalog.json#/components/Button")

    assert '"const": "Button"' in out
    assert "common_types.json#/$defs/Action" in out


def test_catalog_skill_basic_exposes_both_sidecar_and_catalog_file_roots(
    tmp_path: Path,
) -> None:
    """Adversarial-review fix: the Basic catalog's asymmetric layout (sidecar
    files in one directory, catalog.json in another) must not hide either
    root from load_skill -- both the sidecar's instructions.md and the
    catalog file's own component schemas must be reachable."""

    from clio_agent.gact.a2ui_catalogs.builtin import load_builtin_catalogs
    from clio_agent.gact.app import build_app

    app = build_app(sessions_path=tmp_path / "sessions.json")
    session = app.state.sessions.create(workspace_id="ws_default", title="root")
    bind_builtin_catalogs(app, session.id)
    root = session_agent_def(app, session.id)
    rt = skill_runtime_for_agent(app, root, session_id=session.id)
    tool = build_load_skill_tool(root, rt)

    basic, _ = load_builtin_catalogs()
    instructions = tool.func(skill_id="a2ui-catalog-basic", file="instructions.md")
    component = tool.func(skill_id="a2ui-catalog-basic", file="catalog.json#/components/Button")

    assert instructions.strip() == basic.instructions.strip()
    assert '"const": "Button"' in component


def test_catalog_skill_fragment_inlines_local_defs_and_names_standard_refs(
    tmp_path: Path,
) -> None:
    """CatalogComponentCommon is a LOCAL ``$defs`` ref -- one load INLINES it
    (its own ``weight`` property is visible without a second call); an
    external common_types.json ref is only NAMED (this call cannot load it,
    #1533 phase 3)."""

    from clio_agent.gact.app import build_app

    app = build_app(sessions_path=tmp_path / "sessions.json")
    session = app.state.sessions.create(workspace_id="ws_default", title="root")
    root = session_agent_def(app, session.id)
    rt = skill_runtime_for_agent(app, root, session_id=session.id)
    tool = build_load_skill_tool(root, rt)

    out = tool.func(skill_id="a2ui-catalog-clio-workspace", file="catalog.json#/components/Button")

    assert '"$ref": "#/$defs/CatalogComponentCommon"' not in out
    assert '"weight"' in out
    assert "Relative flex weight" in out
    assert "Local refs" not in out
    assert "Standard refs (not loadable here):" in out
    assert "common_types.json#/$defs/Action" in out


def test_catalog_skill_map_fragment_inlines_map_point_and_data_query(tmp_path: Path) -> None:
    """A component that ``$ref``s several of its catalog's own ``$defs`` (map
    references ``MapPoint``, ``DataQuery``, ``FieldName``, and
    ``CatalogComponentCommon``) gets ALL of them inlined by one load."""

    from clio_agent.gact.app import build_app

    app = build_app(sessions_path=tmp_path / "sessions.json")
    session = app.state.sessions.create(workspace_id="ws_default", title="root")
    root = session_agent_def(app, session.id)
    rt = skill_runtime_for_agent(app, root, session_id=session.id)
    tool = build_load_skill_tool(root, rt)

    out = tool.func(
        skill_id="a2ui-catalog-clio-workspace", file="catalog.json#/components/clio.map.v1"
    )

    # MapPoint's own required fields are visible without a second call.
    assert '"latitude"' in out
    assert '"longitude"' in out
    # DataQuery's own nested shape (which itself $refs QueryFilter/QueryAggregate/
    # QueryDownsample) is inlined too -- a multi-level chain, not just one hop.
    assert '"aggregate"' in out
    assert '"downsample"' in out
    assert "per_entity_lttb" in out
    assert '"$ref": "#/$defs/MapPoint"' not in out
    assert '"$ref": "#/$defs/DataQuery"' not in out
    assert '"$ref": "#/$defs/CatalogComponentCommon"' not in out


def test_catalog_skill_fragment_inlines_defs_at_every_nesting_depth(tmp_path: Path) -> None:
    """Regression: a def nested SEVERAL levels inside an already-inlined def
    (DataQuery's own aggregate.metrics[].column -- itself a FieldName $ref,
    reached only after allOf -> properties -> dataQuery -> DataQuery's own
    properties -> aggregate -> anyOf -> QueryAggregate's own properties ->
    metrics -> items -> properties -> column) must still be inlined. There is
    no depth counter at all (an arbitrary number is either too tight -- the
    original bug here -- or meaningless); only a genuine CYCLE is guarded."""

    from clio_agent.gact.app import build_app

    app = build_app(sessions_path=tmp_path / "sessions.json")
    session = app.state.sessions.create(workspace_id="ws_default", title="root")
    root = session_agent_def(app, session.id)
    rt = skill_runtime_for_agent(app, root, session_id=session.id)
    tool = build_load_skill_tool(root, rt)

    out = tool.func(
        skill_id="a2ui-catalog-clio-workspace",
        file="catalog.json#/components/clio.data-table.v1",
    )

    assert '"$ref": "#/$defs/FieldName"' not in out
    assert "Local refs" not in out
    assert "Standard refs (not loadable here):" in out


def test_catalog_skill_fragment_leaves_a_genuinely_recursive_def_as_a_noted_ref(
    tmp_path: Path,
) -> None:
    """The chart spec guard's ``SpecNoForbiddenKeys``/``SpecDataNamedSource``
    $ref THEMSELVES (walking a Vega-Lite spec at any depth) -- a true cycle.
    The outermost reference still inlines (its own if/then/else shape is
    visible); only the SELF-referencing occurrence inside it is left as a
    $ref, annotated as recursive rather than expanded forever."""

    from clio_agent.gact.app import build_app

    app = build_app(sessions_path=tmp_path / "sessions.json")
    session = app.state.sessions.create(workspace_id="ws_default", title="root")
    root = session_agent_def(app, session.id)
    rt = skill_runtime_for_agent(app, root, session_id=session.id)
    tool = build_load_skill_tool(root, rt)

    out = tool.func(
        skill_id="a2ui-catalog-clio-workspace",
        file="catalog.json#/components/clio.chart.v1",
    )

    # The outer reference inlined: SpecNoForbiddenKeys' own if/then/else shape
    # (the "no url/usermeta key" rule) is visible, not just a bare $ref.
    assert '"usermeta"' in out
    # The self-referencing occurrence is left as a $ref, with a note.
    assert '"$ref": "#/$defs/SpecNoForbiddenKeys"' in out
    assert '"$ref": "#/$defs/SpecDataNamedSource"' in out
    assert "recursive:" in out
    assert "already being expanded on this path" in out
    assert (
        "catalog.json#/$defs/SpecNoForbiddenKeys" in out
        and "catalog.json#/$defs/SpecDataNamedSource" in out
    )


def test_load_skill_files_batches_two_real_catalog_components(tmp_path: Path) -> None:
    from clio_agent.gact.app import build_app

    app = build_app(sessions_path=tmp_path / "sessions.json")
    session = app.state.sessions.create(workspace_id="ws_default", title="root")
    root = session_agent_def(app, session.id)
    rt = skill_runtime_for_agent(app, root, session_id=session.id)
    tool = build_load_skill_tool(root, rt)

    out = tool.func(
        skill_id="a2ui-catalog-clio-workspace",
        files=[
            "catalog.json#/components/clio.map.v1",
            "catalog.json#/components/clio.data-table.v1",
        ],
    )

    assert "=== File 1/2: catalog.json#/components/clio.map.v1 ===" in out
    assert "=== File 2/2: catalog.json#/components/clio.data-table.v1 ===" in out
    assert '"const": "clio.map.v1"' in out
    assert '"const": "clio.data-table.v1"' in out


def test_load_skill_files_shares_a_diamond_def_once_instead_of_per_file(pack: Path) -> None:
    """#1533 S4 adversarial review: two components both reaching the SAME
    shared def (directly, or transitively through a second def) must not
    each carry their own fully-inlined copy of it — it is rendered exactly
    once, in a trailing shared-definitions block, and left as a bare $ref
    everywhere else."""

    catalog = {
        "components": {
            "A": {
                "type": "object",
                "properties": {
                    "component": {"const": "A"},
                    "shared": {"$ref": "#/$defs/Shared"},
                },
            },
            "B": {
                "type": "object",
                "properties": {
                    "component": {"const": "B"},
                    "shared": {"$ref": "#/$defs/Shared"},
                },
            },
        },
        "$defs": {
            "Shared": {
                "type": "object",
                "properties": {
                    "marker": {"const": "UNIQUE_SHARED_MARKER_VALUE"},
                    "nested": {"$ref": "#/$defs/Nested"},
                },
            },
            "Nested": {"type": "object", "properties": {"deep": {"const": "UNIQUE_NESTED_MARKER"}}},
        },
    }
    (pack / "skills" / "quality-rubric" / "catalog.json").write_text(
        json.dumps(catalog), encoding="utf-8"
    )
    rt = _runtime(pack)
    tool = build_load_skill_tool(_agent(pack), rt)

    out = tool.func(
        skill_id="quality-rubric",
        files=["catalog.json#/components/A", "catalog.json#/components/B"],
    )

    # Both shared/transitive defs are rendered exactly ONCE across the whole
    # result, never once per component that reaches them.
    assert out.count("UNIQUE_SHARED_MARKER_VALUE") == 1
    assert out.count("UNIQUE_NESTED_MARKER") == 1
    assert "## Shared definitions" in out
    # Each component's own body still names the def by a bare $ref (never
    # silently dropped -- just not re-expanded).
    assert out.count('"$ref": "#/$defs/Shared"') == 2


def test_load_skill_files_all_real_catalog_components_stays_proportional(
    tmp_path: Path,
) -> None:
    """Requesting EVERY component of a real, shared-$defs-heavy catalog in one
    files=[...] call must stay smaller than each component's fully-inlined
    body concatenated separately (the pre-fix behavior) -- proportional to
    what was requested, not requested-files times shared-defs."""

    from clio_agent.gact.app import build_app

    app = build_app(sessions_path=tmp_path / "sessions.json")
    session = app.state.sessions.create(workspace_id="ws_default", title="root")
    root = session_agent_def(app, session.id)
    rt = skill_runtime_for_agent(app, root, session_id=session.id)
    tool = build_load_skill_tool(root, rt)

    resolution = rt.resolved["a2ui-catalog-clio-workspace"]
    assert resolution.skill is not None
    catalog_path = Path(resolution.skill.dir) / "catalog.json"
    document = json.loads(catalog_path.read_text(encoding="utf-8"))
    component_names = sorted(document["components"])
    assert len(component_names) >= 4, "expected the real multi-component workspace catalog"
    fragments = [f"catalog.json#/components/{name}" for name in component_names]

    batched = tool.func(skill_id="a2ui-catalog-clio-workspace", files=fragments)
    separately = sum(
        len(tool.func(skill_id="a2ui-catalog-clio-workspace", file=fragment))
        for fragment in fragments
    )

    assert len(batched) < separately
    for name in component_names:
        assert f'"const": "{name}"' in batched


def test_load_skill_files_collapses_a_duplicate_request_to_one_read(pack: Path) -> None:
    catalog = {"components": {"Button": {"type": "object", "properties": {}}}}
    (pack / "skills" / "quality-rubric" / "catalog.json").write_text(
        json.dumps(catalog), encoding="utf-8"
    )
    rt = _runtime(pack)
    tool = build_load_skill_tool(_agent(pack), rt)

    out = tool.func(
        skill_id="quality-rubric",
        files=["catalog.json#/components/Button", "catalog.json#/components/Button"],
    )

    # A single distinct fragment requested twice collapses to ONE read (the
    # same shape a lone files=[the-one-fragment] call would produce), never
    # duplicated content.
    assert out.count("=== File") == 0
    assert "duplicate file request" in out


def test_load_skill_files_collapses_a_duplicate_alongside_a_distinct_file(pack: Path) -> None:
    catalog = {
        "components": {
            "Button": {"type": "object", "properties": {}},
            "Text": {"type": "object", "properties": {}},
        }
    }
    (pack / "skills" / "quality-rubric" / "catalog.json").write_text(
        json.dumps(catalog), encoding="utf-8"
    )
    rt = _runtime(pack)
    tool = build_load_skill_tool(_agent(pack), rt)

    out = tool.func(
        skill_id="quality-rubric",
        files=[
            "catalog.json#/components/Button",
            "catalog.json#/components/Button",
            "catalog.json#/components/Text",
        ],
    )

    assert "=== File 1/2: catalog.json#/components/Button ===" in out
    assert "=== File 2/2: catalog.json#/components/Text ===" in out
    assert "1 duplicate file request collapsed" in out
