"""Built-in ``planning`` entry-skill + user toggle (P1.5 #1067).

The shipped ``planning`` skill is the first ``enter_mode`` consumer (P1.0): the catalog scan
finds it as a built-in, it declares ``effect: enter_mode:plan``, its body is the four-phase
plan-mode workflow, and invoking it via the skill-effect path enters plan mode and engages the
plan machinery. The USER TOGGLE (``PATCH /v1/sessions`` mode=plan) drives the SAME machinery.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from clio_agent.gact import context as _ctx
from clio_agent.gact.agents.skill_effects import SkillEffect, parse_skill_effect
from clio_agent.gact.agents.skill_runtime import SkillRuntime, build_load_skill_tool
from clio_agent.gact.app import build_app
from clio_agent.gact.plan_mode import PLAN_MODE_REMINDER_MARKER, inject_plan_mode_reminder
from clio_agent.gact.skills import SkillCatalog, read_skill_body
from clio_agent.gact.types import AgentDef

pytestmark = pytest.mark.usefixtures("host_agent_executor")


def _catalog() -> SkillCatalog:
    # The conftest isolation keeps clio's shipped built-in root, so a normally-built catalog
    # resolves the built-in ``planning`` skill (no per-test authoring needed).
    return SkillCatalog()


# ---- the skill exists as a built-in + declares enter_mode:plan -------------------------


def test_planning_skill_is_discovered_as_builtin() -> None:
    res = _catalog().resolve("planning")
    assert res.status == "resolved"
    assert res.skill is not None
    assert res.skill.scope == "builtin"


def test_planning_skill_declares_enter_mode_plan() -> None:
    res = _catalog().resolve("planning")
    assert res.skill is not None
    assert parse_skill_effect(res.skill.meta) == SkillEffect(kind="enter_mode", mode="plan")


def test_planning_skill_body_has_four_phases_and_turn_ending_contract() -> None:
    res = _catalog().resolve("planning")
    assert res.skill is not None
    body = read_skill_body(res.skill)
    for phase in ("Phase 1", "Phase 2", "Phase 3", "Phase 4"):
        assert phase in body, f"planning body missing {phase}"
    # Phase intents (grounding-before-asking, incremental drafting, epistemic ledger).
    assert "do not ask" in body.lower()
    assert "epistemic ledger" in body.lower()
    # The turn-ending contract: end the turn via plan_exit (or the question tool).
    assert "plan_exit" in body
    assert "END YOUR TURN" in body


# ---- invoking the skill via the P1.0 effect path enters plan mode ----------------------


def test_invoking_planning_skill_enters_plan_mode_and_engages_machinery(tmp_path: Path) -> None:
    app = build_app(sessions_path=tmp_path / "s.json")
    sess = app.state.sessions.create(workspace_id="ws_default", title="t", mode="edit")
    agent = AgentDef(id="main", source="expert_pack", title="Main", skills=["planning"], metadata={})
    rt = SkillRuntime(resolutions=_catalog().resolve_declared(["planning"]))
    tool = build_load_skill_tool(agent, rt)

    tok_a = _ctx.set_app(app)
    tok_s = _ctx.set_session_id(sess.id)
    try:
        out = tool.func(skill_id="planning")
    finally:
        _ctx.reset(tok_s)
        _ctx.reset(tok_a)

    # The declared enter_mode effect fired via the runtime (not parsed from body).
    assert app.state.sessions.get(sess.id).mode == "plan"
    assert "skill effect" in out and "Planning" in out
    # The plan machinery now applies: the per-turn reminder is injected in plan mode.
    reminder = inject_plan_mode_reminder(app, sess.id, app.state.sessions.get(sess.id), "USER TURN")
    assert PLAN_MODE_REMINDER_MARKER in reminder


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
        reminder = inject_plan_mode_reminder(app, sid, sess, "USER TURN")
        assert PLAN_MODE_REMINDER_MARKER in reminder
