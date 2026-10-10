"""Visual-review discovery respects expert capabilities and skill precedence."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from clio_agent.gact.agents.skill_runtime import (
    SkillRuntime,
    build_load_skill_tool,
    effective_declared_skills,
    skills_prompt_block,
)
from clio_agent.gact.catalog import _builtin_main_agent
from clio_agent.gact.skills import SkillCatalog
from clio_agent.gact.types import AgentDef

SKILL_ID = "review-visual-presentation"


def test_bike_report_uses_producer_validation_and_conserves_its_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The loadable report is an accepted real catalog document with consistent synthetic counts."""
    from clio_agent.gact.dashboard_document import DashboardDocument, compile_dashboard_surface
    from tests.test_gact.test_a2ui_chart_guard import _producer_session

    app, sid, _ = _producer_session(tmp_path, monkeypatch)
    catalog = SkillCatalog(home=tmp_path / "home", cwd=tmp_path / "workspace")
    skill = catalog.resolve("create-dashboard").skill
    assert skill is not None
    document = DashboardDocument.model_validate_json(
        (Path(skill.dir) / "references/bike-station-report.json").read_bytes()
    )
    surface, _ = compile_dashboard_surface(app, sid, document, "bike-report")
    assert surface
    rows = next(
        component["data"] for component in document.components if component["id"] == "occupancy"
    )
    for field in ["at_6", "at_7", "at_8", "at_815", "at_830", "at_9"]:
        assert sum(row[field] for row in rows) == 54
        assert all(0 <= row[field] <= row["capacity"] for row in rows)
    assert sum(row["net_bikes"] for row in rows if row["category"] == "Hill") == -37
    assert sum(row["net_bikes"] for row in rows if row["category"] == "Downtown") == 37


@pytest.mark.parametrize(
    "tool_name",
    [
        "create_a2ui_surface",
        "update_a2ui_components",
        "update_a2ui_data_model",
        "publish_dashboard_report",
    ],
)
def test_producer_expert_can_load_review_and_bundled_example(
    tmp_path: Path, tool_name: str
) -> None:
    """A curated producer gets usable review guidance without a full builtin skill list."""
    agent = AgentDef(
        id="analyst", title="Analyst", source="expert_pack", parent_id="main", tools=[tool_name]
    )
    catalog = SkillCatalog(home=tmp_path / "home", cwd=tmp_path / "workspace")
    declared = effective_declared_skills(agent, catalog)
    assert declared == [
        "present-interactive-analysis",
        SKILL_ID,
        *(["create-dashboard"] if tool_name == "publish_dashboard_report" else []),
    ]
    runtime = SkillRuntime(resolutions=catalog.resolve_declared(declared))
    resolution = runtime.resolved[SKILL_ID]
    assert resolution.skill is not None and resolution.skill.scope == "builtin"
    tool = build_load_skill_tool(agent, runtime)
    assert tool.func(skill_id=SKILL_ID)
    example = json.loads(tool.func(skill_id=SKILL_ID, file="examples/annotated-scatter.json"))
    assert example["component"] == "clio.chart.v1"
    assert example["spec"]["data"] == {"name": "source"}
    assert len(example["data"]) == 4


def test_unrelated_expert_does_not_gain_visual_review(tmp_path: Path) -> None:
    """Non-producer expertise keeps its explicit skill surface."""
    agent = AgentDef(
        id="analyst", title="Analyst", source="expert_pack", parent_id="main", tools=["read_file"]
    )
    catalog = SkillCatalog(home=tmp_path / "home", cwd=tmp_path / "workspace")
    assert effective_declared_skills(agent, catalog) == []


def test_main_discovers_review_and_workspace_override_wins(tmp_path: Path) -> None:
    """The default main sees review metadata while workspace guidance keeps precedence."""
    catalog = SkillCatalog(home=tmp_path / "home", cwd=tmp_path / "workspace")
    assert SKILL_ID in effective_declared_skills(_builtin_main_agent(), catalog)
    custom = tmp_path / "workspace" / ".agents" / "skills" / SKILL_ID / "SKILL.md"
    custom.parent.mkdir(parents=True)
    custom.write_text(
        f"---\nname: {SKILL_ID}\ndescription: Project visual review\n---\nProject procedure.\n",
        encoding="utf-8",
    )
    catalog = SkillCatalog(home=tmp_path / "home", cwd=tmp_path / "workspace")
    agent = AgentDef(
        id="analyst",
        title="Analyst",
        source="expert_pack",
        parent_id="main",
        tools=["create_a2ui_surface"],
        skills=[SKILL_ID],
    )
    declared = effective_declared_skills(agent, catalog)
    assert declared == [SKILL_ID, "present-interactive-analysis"]
    runtime = SkillRuntime(resolutions=catalog.resolve_declared(declared))
    assert runtime.resolved[SKILL_ID].skill is not None
    assert runtime.resolved[SKILL_ID].skill.scope == "workspace"
    assert "Project procedure." in build_load_skill_tool(agent, runtime).func(skill_id=SKILL_ID)


@pytest.mark.parametrize("agent_id", ["appl", "factorio", "earthscope", "custom-scientist"])
def test_automatic_producer_roots_receive_shared_loadable_guidance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, agent_id: str
) -> None:
    """Any catalog-enabled root gets current guidance, independently of its pack name."""
    from tests.test_gact.test_a2ui_chart_guard import _producer_session

    app, sid, _ = _producer_session(tmp_path, monkeypatch)
    agent = AgentDef(id=agent_id, title="Scientific collaborator", source="expert_pack")
    catalog = SkillCatalog(
        home=tmp_path / "home", cwd=tmp_path / "workspace", app=app, session_id=sid
    )
    declared = effective_declared_skills(agent, catalog, app=app, session_id=sid)
    assert any(skill.startswith("a2ui-catalog-") for skill in declared)
    assert {"present-interactive-analysis", "create-dashboard", SKILL_ID} <= set(declared)
    runtime = SkillRuntime(resolutions=catalog.resolve_declared(declared))
    prompt = skills_prompt_block(runtime)
    assert "Review matching pixels at the user's viewing size" in prompt
    tool = build_load_skill_tool(agent, runtime)
    assert "Surface acceptance alone" in tool.func(skill_id="create-dashboard")
    design = tool.func(skill_id="create-dashboard", file="references/report-design.md")
    assert "Compose related evidence in one initial space" in design
    assert "inspect the resulting image, reason, and refine" in tool.func(skill_id=SKILL_ID)


def test_root_without_producible_catalogs_keeps_explicit_skills(tmp_path: Path) -> None:
    """Being a custom root does not invent presentation capabilities or procedures."""
    from fastapi import FastAPI

    app = FastAPI()
    catalog = SkillCatalog(home=tmp_path / "home", cwd=tmp_path / "workspace")
    agent = AgentDef(id="custom", title="Text analyst", skills=["planning"])
    assert effective_declared_skills(agent, catalog, app=app, session_id="session") == ["planning"]


def test_annotation_example_uses_real_producer_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The shipped example revises an existing chart without changing its base data."""
    from clio_agent.gact.a2ui_producer import (
        build_inspect_a2ui_surface_tool,
        build_update_a2ui_components_tool,
    )
    from tests.test_gact.test_a2ui_chart_guard import _create, _producer_session

    _producer_session(tmp_path, monkeypatch)
    catalog = SkillCatalog(home=tmp_path / "home", cwd=tmp_path / "workspace")
    resolution = catalog.resolve(SKILL_ID)
    assert resolution.skill is not None
    example: dict[str, Any] = json.loads(
        (Path(resolution.skill.dir) / "examples/annotated-scatter.json").read_text(encoding="utf-8")
    )
    base = {
        **example,
        "spec": {
            "data": {"name": "source"},
            "mark": "point",
            "encoding": example["spec"]["encoding"],
        },
    }
    created = _create([base])
    assert created.get("ok") is not False, created
    revised = build_update_a2ui_components_tool()(surface_id="surface", components=[example])
    assert revised.get("ok") is not False, revised
    assert revised["revision"] > created["revision"]
    inspected = build_inspect_a2ui_surface_tool()(surface_id="surface")
    chart = inspected["components"][0]
    assert chart["data"] == base["data"]
    assert chart["spec"]["layer"] == example["spec"]["layer"]
    assert revised["definition_artifact_id"]
