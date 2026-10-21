"""Visual-review guidance comes from the ``visuals`` plugin an agent carries.

The visual skills (``review-visual-presentation``, ``create-dashboard``,
``present-interactive-analysis``, ``create-diagram``) ship in the marketplace's
``visuals`` plugin. An agent gets them -- and the A2UI producer tools -- exactly
when its resolution carries that plugin; one without it keeps only its own
skills. These tests run the REAL ``visuals`` plugin from the pinned marketplace
submodule (copied into this test's default marketplace in place of the hermetic
stand-in) so the shipped guidance and examples are checked against the real
producer validation.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from clio_agent.gact import context as gact_context
from clio_agent.gact.a2ui_capabilities import remember_client_capabilities
from clio_agent.gact.a2ui_catalogs.builtin import workspace_catalog_id
from clio_agent.gact.agents.resolution import pin_session_agent, session_agent_def
from clio_agent.gact.agents.skill_runtime import (
    SkillRuntime,
    build_load_skill_tool,
    effective_declared_skills,
    skill_runtime_for_agent,
    skills_prompt_block,
)
from clio_agent.gact.app import build_app
from clio_agent.gact.skills import SkillCatalog
from clio_agent.gact.types import AgentDef
from clio_agent.plugins.resolve import Delta
from tests._marketplace import MARKETPLACE_ROOT
from tests._plugin_fixtures import custom_marketplace

pytestmark = [pytest.mark.marketplace, pytest.mark.usefixtures("clio_core_plane")]

SKILL_ID = "review-visual-presentation"
VISUAL_SKILLS = {"present-interactive-analysis", "create-dashboard", "create-diagram", SKILL_ID}
REAL_VISUALS = MARKETPLACE_ROOT / "plugins" / "visuals"
WORKSPACE_ID = workspace_catalog_id()

_SCIENTIST_AGENT = """
from clio_agent.sdk import Agent


class CustomScientist(Agent):
    id = "custom-scientist"
    version = "1.0.0"
    name = "Scientific collaborator"
    description = "A custom root that carries only core and visuals."
    plugins = Agent.plugins + ["visuals"]
"""


@pytest.fixture
def real_visuals(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Make the default marketplace carry the REAL ``visuals`` plugin."""

    root = custom_marketplace(
        tmp_path, monkeypatch, agents={"custom_scientist.py": _SCIENTIST_AGENT}
    )
    shutil.rmtree(root / "plugins" / "visuals")
    shutil.copytree(
        REAL_VISUALS,
        root / "plugins" / "visuals",
        ignore=shutil.ignore_patterns("__pycache__", "tests"),
    )
    return root


def _visual_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, agent_ref: str = "clio/standard"
) -> tuple[Any, str]:
    """An app + session on ``agent_ref``, wired as the producer tools read it."""

    from clio_schemas.a2ui.v0_9_1.capabilities import A2UIClientCapabilities  # noqa: PLC0415

    app = build_app(sessions_path=tmp_path / "sessions.json")
    app.state.workspaces.update("ws_default", root_path=str(tmp_path))
    session = app.state.sessions.create(workspace_id="ws_default", title="visual review")
    pin_session_agent(app, session.id, ref=agent_ref)
    monkeypatch.setattr(gact_context, "active_app", lambda: app)
    monkeypatch.setattr(gact_context, "active_session_id", lambda: session.id)
    caps = A2UIClientCapabilities.model_validate({"v0.9": {"supportedCatalogIds": [WORKSPACE_ID]}})
    remember_client_capabilities(app, session.id, caps)
    return app, session.id


def _runtime(app: Any, sid: str) -> tuple[AgentDef, SkillRuntime]:
    agent = session_agent_def(app, sid)
    return agent, skill_runtime_for_agent(app, agent, session_id=sid)


def test_bike_report_uses_producer_validation_and_conserves_its_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, real_visuals: Path
) -> None:
    """The loadable report is an accepted real catalog document with consistent synthetic counts."""
    from clio_agent.gact.dashboard_document import DashboardDocument, compile_dashboard_surface

    app, sid = _visual_session(tmp_path, monkeypatch)
    _agent, runtime = _runtime(app, sid)
    skill = runtime.resolved["create-dashboard"].skill
    assert skill is not None and skill.scope == "plugin"
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


def test_producer_agent_can_load_review_and_bundled_example(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, real_visuals: Path
) -> None:
    """An agent carrying ``visuals`` gets the producer tools and usable review guidance."""

    app, sid = _visual_session(tmp_path, monkeypatch)
    agent, runtime = _runtime(app, sid)

    assert {"create_a2ui_surface", "publish_dashboard_report"} <= set(agent.tools)
    assert VISUAL_SKILLS <= set(runtime.resolved)
    resolution = runtime.resolved[SKILL_ID]
    assert resolution.skill is not None and resolution.skill.scope == "plugin"
    tool = build_load_skill_tool(agent, runtime)
    assert tool.func(skill_id=SKILL_ID)
    example = json.loads(tool.func(skill_id=SKILL_ID, file="examples/annotated-scatter.json"))
    assert example["component"] == "clio.chart.v1"
    assert example["spec"]["data"] == {"name": "source"}
    assert len(example["data"]) == 4


def test_agent_without_visuals_does_not_gain_visual_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, real_visuals: Path
) -> None:
    """Without the ``visuals`` plugin an agent keeps its own explicit skill surface."""

    app, sid = _visual_session(tmp_path, monkeypatch)
    pin_session_agent(app, sid, delta=Delta(remove=("visuals",)))
    agent, runtime = _runtime(app, sid)

    assert "create_a2ui_surface" not in agent.tools
    assert not VISUAL_SKILLS & set(runtime.resolutions)
    assert not [skill for skill in runtime.resolutions if skill.startswith("a2ui-catalog-")]


def test_plugin_review_skill_shadows_a_workspace_skill_of_the_same_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, real_visuals: Path
) -> None:
    """The default agent sees the review skill; the resolved plugin's copy leads a
    same-id workspace skill (``gact/skills.py`` scope precedence: plugin first)."""

    app, sid = _visual_session(tmp_path, monkeypatch)
    agent, runtime = _runtime(app, sid)
    assert SKILL_ID in runtime.resolved
    custom = tmp_path / "workspace" / ".agents" / "skills" / SKILL_ID / "SKILL.md"
    custom.parent.mkdir(parents=True)
    custom.write_text(
        f"---\nname: {SKILL_ID}\ndescription: Project visual review\n---\nProject procedure.\n",
        encoding="utf-8",
    )
    folders = [tuple(row) for row in agent.metadata["skill_folders"]]
    catalog = SkillCatalog(
        home=tmp_path / "home", cwd=tmp_path / "workspace", plugin_skills=folders
    )

    resolution = catalog.resolve(SKILL_ID)

    assert resolution.skill is not None
    assert resolution.skill.scope == "plugin"
    assert "Project procedure." not in (resolution.skill.body or "")


@pytest.mark.parametrize("agent_ref", ["clio/standard", "clio/custom-scientist"])
def test_producer_roots_receive_shared_loadable_guidance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, real_visuals: Path, agent_ref: str
) -> None:
    """Any root carrying ``visuals`` gets the current guidance, whatever the agent is."""

    app, sid = _visual_session(tmp_path, monkeypatch, agent_ref)
    agent, runtime = _runtime(app, sid)
    declared = effective_declared_skills(
        agent,
        SkillCatalog(
            home=tmp_path / "home",
            cwd=tmp_path / "workspace",
            app=app,
            session_id=sid,
            plugin_skills=[tuple(row) for row in agent.metadata["skill_folders"]],
        ),
        app=app,
        session_id=sid,
    )
    assert any(skill.startswith("a2ui-catalog-") for skill in declared)
    assert VISUAL_SKILLS <= set(declared)
    prompt = skills_prompt_block(runtime)
    assert "Review matching pixels at the user's viewing size" in prompt
    tool = build_load_skill_tool(agent, runtime)
    assert "Surface acceptance alone" in tool.func(skill_id="create-dashboard")
    design = tool.func(skill_id="create-dashboard", file="references/report-design.md")
    assert "Compose related evidence in one initial space" in design
    assert "inspect the resulting image, reason, and refine" in tool.func(skill_id=SKILL_ID)


def test_root_without_producer_tools_keeps_explicit_skills(tmp_path: Path) -> None:
    """Being a root does not invent presentation capabilities or procedures."""
    from fastapi import FastAPI

    app = FastAPI()
    catalog = SkillCatalog(home=tmp_path / "home", cwd=tmp_path / "workspace")
    agent = AgentDef(id="custom", title="Text analyst", skills=["planning"])
    assert effective_declared_skills(agent, catalog, app=app, session_id="session") == ["planning"]


def test_annotation_example_uses_real_producer_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, real_visuals: Path
) -> None:
    """The shipped example revises an existing chart without changing its base data."""
    from clio_agent.gact.a2ui_producer import (
        build_create_a2ui_surface_tool,
        build_inspect_a2ui_surface_tool,
        build_update_a2ui_components_tool,
    )

    producer_app, sid = _visual_session(tmp_path, monkeypatch)
    with TestClient(producer_app):
        _agent, runtime = _runtime(producer_app, sid)
        resolution = runtime.resolved[SKILL_ID]
        assert resolution.skill is not None
        example: dict[str, Any] = json.loads(
            (Path(resolution.skill.dir) / "examples/annotated-scatter.json").read_text(
                encoding="utf-8"
            )
        )
        base = {
            **example,
            "spec": {
                "data": {"name": "source"},
                "mark": "point",
                "encoding": example["spec"]["encoding"],
            },
        }
        created = build_create_a2ui_surface_tool()(surface_id="surface", components=[base])
        assert created.get("ok") is not False, created
        revised = build_update_a2ui_components_tool()(surface_id="surface", components=[example])
        assert revised.get("ok") is not False, revised
        assert revised["revision"] > created["revision"]
        inspected = build_inspect_a2ui_surface_tool()(surface_id="surface")
        chart = inspected["components"][0]
        assert chart["data"] == base["data"]
        assert chart["spec"]["layer"] == example["spec"]["layer"]
        assert revised["definition_artifact_id"]
