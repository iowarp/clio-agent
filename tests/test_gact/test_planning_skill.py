"""The ``core`` plugin's ``planning`` entry-skill + user toggle (P1.5 #1067).

The shipped ``planning`` skill is the first ``enter_mode`` consumer (P1.0): the catalog scan
finds it in the ``core`` plugin every agent carries, it declares ``effect: enter_mode:plan``, its body is the four-phase
plan-mode workflow, and invoking it via the skill-effect path enters plan mode and engages the
plan machinery. The USER TOGGLE (``PATCH /v1/sessions`` mode=plan) drives the SAME machinery.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

import clio_agent.plugins
from clio_agent.gact import context as _ctx
from clio_agent.gact.agents.skill_effects import parse_skill_effect
from clio_agent.gact.agents.skill_runtime import SkillRuntime, build_load_skill_tool
from clio_agent.gact.app import build_app
from clio_agent.gact.plan_mode import (
    PLAN_MODE_REMINDER_MARKER,
    plan_mode_reminder,
    recorded_plan_file,
)
from clio_agent.gact.skills import SkillCatalog, read_skill_body
from clio_agent.gact.types import AgentDef

pytestmark = pytest.mark.usefixtures("host_agent_executor")

CORE_SKILLS = Path(clio_agent.plugins.__file__).resolve().parent / "builtin" / "core" / "skills"


def _catalog() -> SkillCatalog:
    # ``planning`` ships in the core plugin (every agent carries it).
    return SkillCatalog(plugin_skills=[("core", str(CORE_SKILLS))])


# ---- the skill exists as a built-in + declares enter_mode:plan -------------------------


def test_planning_skill_is_discovered_in_the_core_plugin() -> None:
    res = _catalog().resolve("planning")
    assert res.status == "resolved"
    assert res.skill is not None
    assert res.skill.scope == "plugin"


def test_planning_skill_is_a_procedure_without_a_mode_effect() -> None:
    """plan_enter is the one way into plan mode; the skill describes the procedure."""
    res = _catalog().resolve("planning")
    assert res.skill is not None
    assert parse_skill_effect(res.skill.meta) is None
    assert "plan_enter" in res.skill.description


def test_planning_skill_body_has_four_phases_and_turn_ending_contract() -> None:
    res = _catalog().resolve("planning")
    assert res.skill is not None
    body = read_skill_body(res.skill)
    for phase in ("Phase 1", "Phase 2", "Phase 3", "Phase 4"):
        assert phase in body, f"planning body missing {phase}"
    # Phase intents (grounding-before-asking, incremental drafting, epistemic ledger).
    assert "do not ask" in body.lower()
    assert "epistemic ledger" in body.lower()
    # Entry, drafting and the turn-ending contract use the plan tools.
    assert "plan_enter" in body and "plan_update" in body
    assert "plan_exit" in body
    assert "end your turn" in body.lower()


# ---- loading the skill leaves the mode alone; plan_enter enters plan mode ---------------


def test_loading_the_planning_skill_does_not_change_the_mode(tmp_path: Path) -> None:
    app = build_app(sessions_path=tmp_path / "s.json")
    sess = app.state.sessions.create(workspace_id="ws_default", title="t", mode="edit")
    agent = AgentDef(id="standard", source="plugin", title="CLIO", skills=["planning"], metadata={})
    rt = SkillRuntime(resolutions=_catalog().resolve_declared(["planning"]))
    tool = build_load_skill_tool(agent, rt)

    tok_a = _ctx.set_app(app)
    tok_s = _ctx.set_session_id(sess.id)
    try:
        out = tool.func(skill_id="planning")
    finally:
        _ctx.reset(tok_s)
        _ctx.reset(tok_a)

    assert app.state.sessions.get(sess.id).mode == "edit"
    assert "skill effect" not in out and "plan_enter" in out


def test_plan_enter_enters_plan_mode_and_engages_machinery(tmp_path: Path) -> None:
    """Mid-turn entry cannot wait for the next turn's reminder: plan_enter's result names the
    plan path, with its parent ready, and the model authors the plan."""
    from clio_agent.gact.session_plan import enter_plan

    app = build_app(sessions_path=tmp_path / "s.json")
    sess = app.state.sessions.create(workspace_id="ws_default", title="t", mode="edit")
    out = enter_plan(app, sess.id, "an open question")
    fresh = app.state.sessions.get(sess.id)
    assert fresh.mode == "plan"
    plan_file = recorded_plan_file(fresh)
    assert plan_file is not None
    assert PLAN_MODE_REMINDER_MARKER in out
    assert plan_file in out
    assert Path(plan_file).parent.is_dir()
    assert not Path(plan_file).exists(), "the model, not the runtime, authors the plan"


# ---- user toggle: PATCH mode=plan drives the same machinery end-to-end -----------------


def test_user_toggle_patch_mode_plan_engages_enforcement(tmp_path: Path) -> None:
    """The USER-driven entry (PATCH /v1/sessions mode=plan) drives the same plan machinery:
    plan_acl denies a write and the reminder is injected — end-to-end, no skill involved."""

    from clio_agent.gact.permission_gate import _policy_action_for_tool

    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as c:
        sid = c.post("/v1/sessions", json={"title": "toggle"}).json()["id"]
        assert app.state.sessions.get(sid).mode == "edit"

        patched = c.patch(f"/v1/sessions/{sid}", json={"mode": "plan"})
        assert patched.status_code == 200
        assert patched.json()["mode"] == "plan"

        sess: Any = app.state.sessions.get(sid)
        # plan_acl (the unified resolver) denies a write tool once the user toggled plan mode.
        action = _policy_action_for_tool(
            app,
            session_id=sid,
            session=sess,
            tool_name="shell.exec",
            args={"cmd": "rm -rf /"},
            mode=sess.mode,
        )
        assert action == "deny"
        # And the per-turn plan reminder is injected for the user-toggled session.
        reminder = plan_mode_reminder(app, sid, sess)
        assert PLAN_MODE_REMINDER_MARKER in reminder
