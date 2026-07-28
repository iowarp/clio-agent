"""Skill privileged-effects substrate (P1.0 #1062).

A skill may declare a structured, runtime-executed EFFECT in its frontmatter
(``enter_mode`` / ``spawn_subagent_with_skill``). The RUNTIME performs the effect when the
skill is invoked — never parsed from the body/model output (injection-safe). enter_mode may
only TIGHTEN the mode (no escape from plan mode). An effect-less skill is unchanged.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest

from clio_agent.gact import context as _ctx
from clio_agent.gact.agents.skill_effects import (
    SkillEffect,
    SkillEffectError,
    SkillModeTransitionError,
    parse_skill_effect,
)
from clio_agent.gact.agents.skill_runtime import SkillRuntime, build_load_skill_tool
from clio_agent.gact.app import build_app
from clio_agent.gact.skills import SkillCatalog
from clio_agent.gact.types import AgentDef

pytestmark = pytest.mark.usefixtures("host_agent_executor")


class _Agent:
    """Stub host agent: a spawned child turn runs a real turn cycle over this."""

    def forward(self, question: str, session_id: str, **_kw: Any) -> Any:
        return type(
            "P",
            (),
            {"answer": f"child ran: {question[:24]}", "selected_expert": "", "routing_rationale": ""},
        )()


def _write_skill(ws: Path, skill_id: str, frontmatter: str, body: str) -> None:
    d = ws / ".claude" / "skills" / skill_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {skill_id}\n{frontmatter}---\n\n{body}\n", encoding="utf-8"
    )


def _tool(ws: Path, skill_id: str, *, agent_id: str = "main") -> Any:
    agent = AgentDef(id=agent_id, source="expert_pack", title="A", skills=[skill_id], metadata={})
    catalog = SkillCatalog(home=ws / "no-home", cwd=ws)
    rt = SkillRuntime(resolutions=catalog.resolve_declared([skill_id]))
    return build_load_skill_tool(agent, rt)


def _message_text(msg: Any) -> str:
    return "".join(
        str(getattr(p, "text", "") or "")
        for p in getattr(msg, "parts", []) or []
        if getattr(p, "type", "") == "text"
    )


def _wait_terminal(app: Any, task_id: str, timeout: float = 10.0) -> Any:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        t = app.state.agent_task_registry.get(task_id)
        if t is not None and t.is_terminal:
            return t
        time.sleep(0.05)
    return app.state.agent_task_registry.get(task_id)


# ---- parse / validation (pure) ---------------------------------------------------------


def test_parse_inline_object_form() -> None:
    effect = parse_skill_effect({"effect": '{kind: "enter_mode", mode: "plan"}'})
    assert effect == SkillEffect(kind="enter_mode", mode="plan")


def test_parse_flat_form_with_sibling_keys() -> None:
    effect = parse_skill_effect({"effect": "enter_mode", "effect_mode": "architect"})
    assert effect == SkillEffect(kind="enter_mode", mode="architect")


def test_parse_spawn_form_agent_optional() -> None:
    assert parse_skill_effect({"effect": "spawn_subagent_with_skill"}) == SkillEffect(
        kind="spawn_subagent_with_skill"
    )
    assert parse_skill_effect(
        {"effect": '{kind: "spawn_subagent_with_skill", agent: "data"}'}
    ) == SkillEffect(kind="spawn_subagent_with_skill", agent="data")


def test_parse_no_effect_is_none() -> None:
    assert parse_skill_effect({"name": "x", "description": "y"}) is None


def test_parse_unknown_kind_is_typed_error() -> None:
    with pytest.raises(SkillEffectError) as exc:
        parse_skill_effect({"effect": "delete_everything"})
    assert exc.value.reason == "unknown_effect_kind"


def test_parse_invalid_mode_is_typed_error() -> None:
    with pytest.raises(SkillEffectError) as exc:
        parse_skill_effect({"effect": '{kind: "enter_mode", mode: "chat"}'})
    assert exc.value.reason == "invalid_mode"


# ---- enter_mode effect (via the real session-update path) -------------------------------


def test_enter_mode_effect_sets_plan_and_enforcement_applies(tmp_path: Path) -> None:
    """Invoking a skill declaring effect enter_mode:plan sets session.mode=plan via the real
    path; the plan-mode reminder/enforcement machinery then applies."""

    from clio_agent.gact.plan_mode import PLAN_MODE_REMINDER_MARKER, inject_plan_mode_reminder

    ws = tmp_path / "ws"
    _write_skill(
        ws, "enter-plan", 'effect: {kind: "enter_mode", mode: "plan"}\n', "PLAN_BODY_MARKER."
    )
    app = build_app(sessions_path=tmp_path / "s.json")
    sess = app.state.sessions.create(workspace_id="ws_default", title="t", mode="edit")
    tool = _tool(ws, "enter-plan")

    tok_a = _ctx.set_app(app)
    tok_s = _ctx.set_session_id(sess.id)
    try:
        out = tool.func(skill_id="enter-plan")
    finally:
        _ctx.reset(tok_s)
        _ctx.reset(tok_a)

    # Mode changed via the real session store.
    assert app.state.sessions.get(sess.id).mode == "plan"
    # Confirmation + body both returned (the entered mode's instructions).
    assert "skill effect" in out and "PLAN_BODY_MARKER." in out
    # The plan-mode enforcement machinery now applies to this session.
    fresh = app.state.sessions.get(sess.id)
    reminder = inject_plan_mode_reminder(app, sess.id, fresh, "USER TURN")
    assert PLAN_MODE_REMINDER_MARKER in reminder


def test_enter_mode_can_tighten_edit_to_architect(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    _write_skill(ws, "go-architect", "effect: enter_mode\neffect_mode: architect\n", "BODY.")
    app = build_app(sessions_path=tmp_path / "s.json")
    sess = app.state.sessions.create(workspace_id="ws_default", title="t", mode="edit")
    tool = _tool(ws, "go-architect")
    tok_a = _ctx.set_app(app)
    tok_s = _ctx.set_session_id(sess.id)
    try:
        tool.func(skill_id="go-architect")
    finally:
        _ctx.reset(tok_s)
        _ctx.reset(tok_a)
    assert app.state.sessions.get(sess.id).mode == "architect"


def test_enter_mode_cannot_weaken_out_of_plan(tmp_path: Path) -> None:
    """The no-escape guard: enter_mode:edit invoked while in plan mode is REJECTED — mode
    unchanged, typed reason. Exiting plan mode is the user-gated plan_exit flow (P1.4)."""

    ws = tmp_path / "ws"
    _write_skill(
        ws, "escape-hatch", 'effect: {kind: "enter_mode", mode: "edit"}\n', "ESCAPE_BODY."
    )
    app = build_app(sessions_path=tmp_path / "s.json")
    sess = app.state.sessions.create(workspace_id="ws_default", title="t", mode="plan")
    tool = _tool(ws, "escape-hatch")

    tok_a = _ctx.set_app(app)
    tok_s = _ctx.set_session_id(sess.id)
    try:
        with pytest.raises(SkillModeTransitionError) as exc:
            tool.func(skill_id="escape-hatch")
    finally:
        _ctx.reset(tok_s)
        _ctx.reset(tok_a)

    assert exc.value.reason == "mode_relax_denied"
    # Mode is UNCHANGED — the update path was never reached.
    assert app.state.sessions.get(sess.id).mode == "plan"


# ---- spawn_subagent_with_skill effect --------------------------------------------------


def test_spawn_subagent_with_skill_seeds_body_not_inlined(tmp_path: Path) -> None:
    """Invoking a skill declaring spawn_subagent_with_skill spawns a child turn seeded with
    the skill body (task handle returned; body NOT inlined into the caller)."""

    from fastapi.testclient import TestClient

    ws = tmp_path / "ws"
    _write_skill(
        ws, "research-sub", "effect: spawn_subagent_with_skill\n", "SUBAGENT_SKILL_BODY_MARKER."
    )
    app = build_app(sessions_path=tmp_path / "s.json", agent=_Agent())
    with TestClient(app):
        sess = app.state.sessions.create(workspace_id="ws_default", title="p")
        tool = _tool(ws, "research-sub", agent_id="main")
        tok_a = _ctx.set_app(app)
        tok_s = _ctx.set_session_id(sess.id)
        try:
            out = tool.func(skill_id="research-sub")
        finally:
            _ctx.reset(tok_s)
            _ctx.reset(tok_a)

        data = json.loads(out)
        assert data["skill_effect"] == "spawn_subagent_with_skill"
        assert data["task_id"] and data["child_session_id"]
        # The body is NOT inlined into the caller's tool observation.
        assert "SUBAGENT_SKILL_BODY_MARKER." not in out

        # A real task/handle exists.
        task = app.state.agent_task_registry.get(data["task_id"])
        assert task is not None

        _wait_terminal(app, data["task_id"])
        # The child turn was SEEDED with the skill body (staged as its user input).
        child_msgs = app.state.messages.get(data["child_session_id"], []) or []
        seeded = [m for m in child_msgs if getattr(m, "role", "") == "user"]
        assert seeded, "child got no staged user message"
        assert any("SUBAGENT_SKILL_BODY_MARKER." in _message_text(m) for m in seeded)


def test_spawn_subagent_with_skill_from_plan_mode_parent_inherits_plan_mode(
    tmp_path: Path,
) -> None:
    """Plan-override bypass fix (governance-surfaces P1.1, "subagents inherit
    structurally"): a spawn_subagent_with_skill effect invoked from a PLAN-mode
    parent must spawn a child session that is ALSO plan mode — not the default
    edit — else a plan-mode session could use this effect to spawn a
    full-authority child and write what the parent itself is denied. The fix
    lives in the SHARED spawn path (turn_spawn.spawn_child_turn), so this proves
    it covers the skill-effect caller too, not just the normal spawn tool.
    """

    from fastapi.testclient import TestClient

    from clio_agent.gact.permission_gate import _policy_action_for_tool

    ws = tmp_path / "ws"
    _write_skill(
        ws, "research-sub", "effect: spawn_subagent_with_skill\n", "SUBAGENT_SKILL_BODY_MARKER."
    )
    app = build_app(sessions_path=tmp_path / "s.json", agent=_Agent())
    with TestClient(app):
        sess = app.state.sessions.create(workspace_id="ws_default", title="p", mode="plan")
        tool = _tool(ws, "research-sub", agent_id="main")
        tok_a = _ctx.set_app(app)
        tok_s = _ctx.set_session_id(sess.id)
        try:
            out = tool.func(skill_id="research-sub")
        finally:
            _ctx.reset(tok_s)
            _ctx.reset(tok_a)

        data = json.loads(out)
        child_id = data["child_session_id"]
        child = app.state.sessions.get(child_id)

        assert child.mode == "plan", (
            f"child spawned from a plan-mode parent via spawn_subagent_with_skill "
            f"must inherit plan mode, got {child.mode!r}"
        )
        action = _policy_action_for_tool(
            app,
            session_id=child.id,
            session=child,
            tool_name="shell.exec",
            args={"cmd": "rm -rf /"},
            mode=child.mode,
        )
        assert action == "deny", (
            f"a write tool resolved for the spawned child must be plan_acl-denied (got {action!r})"
        )


# ---- injection-safety ------------------------------------------------------------------


def test_body_effect_text_has_zero_effect(tmp_path: Path) -> None:
    """A skill whose BODY contains the text 'effect: enter_mode' (but no DECLARED effect in
    frontmatter) does NOTHING to the mode — only declared metadata triggers a runtime effect."""

    ws = tmp_path / "ws"
    _write_skill(
        ws,
        "innocent",
        "description: A normal skill\n",
        "Do the thing.\n\neffect: enter_mode\nmode: plan\n\nThat text is just prose.",
    )
    app = build_app(sessions_path=tmp_path / "s.json")
    sess = app.state.sessions.create(workspace_id="ws_default", title="t", mode="edit")
    tool = _tool(ws, "innocent")

    tok_a = _ctx.set_app(app)
    tok_s = _ctx.set_session_id(sess.id)
    try:
        out = tool.func(skill_id="innocent")
    finally:
        _ctx.reset(tok_s)
        _ctx.reset(tok_a)

    # Mode is unchanged and the body loaded as plain text.
    assert app.state.sessions.get(sess.id).mode == "edit"
    assert "That text is just prose." in out


def test_unknown_effect_kind_raises_on_invocation(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    _write_skill(ws, "bad-effect", "effect: nuke_everything\n", "BODY.")
    app = build_app(sessions_path=tmp_path / "s.json")
    sess = app.state.sessions.create(workspace_id="ws_default", title="t", mode="edit")
    tool = _tool(ws, "bad-effect")

    tok_a = _ctx.set_app(app)
    tok_s = _ctx.set_session_id(sess.id)
    try:
        with pytest.raises(SkillEffectError) as exc:
            tool.func(skill_id="bad-effect")
    finally:
        _ctx.reset(tok_s)
        _ctx.reset(tok_a)
    assert exc.value.reason == "unknown_effect_kind"
    # Nothing happened to the mode.
    assert app.state.sessions.get(sess.id).mode == "edit"


# ---- backward-compat -------------------------------------------------------------------


def test_effectless_skill_loads_as_plain_text(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    _write_skill(ws, "plain", "description: Just a rubric\n", "PLAIN_BODY_MARKER. Step one.")
    app = build_app(sessions_path=tmp_path / "s.json")
    sess = app.state.sessions.create(workspace_id="ws_default", title="t", mode="edit")
    tool = _tool(ws, "plain")

    tok_a = _ctx.set_app(app)
    tok_s = _ctx.set_session_id(sess.id)
    try:
        out = tool.func(skill_id="plain")
    finally:
        _ctx.reset(tok_s)
        _ctx.reset(tok_a)

    assert "PLAIN_BODY_MARKER. Step one." in out
    assert "skill effect" not in out
    assert app.state.sessions.get(sess.id).mode == "edit"
