"""write_todos execution-phase TODO tool + recitation + separation (P1.5 #1067).

``write_todos`` is the execution-phase checklist, SEPARATE from plan mode: it is forbidden in
plan mode (mirror of ``plan_exit`` being forbidden outside plan mode), state lives on
``session.metadata['todos']`` (no fifth store), each call is a whole-list replacement, two calls
in one step are rejected (ambiguous), and the list is recited into execution-mode context but
never in plan mode.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

import pytest

from clio_agent.gact import context as _ctx
from clio_agent.gact.app import build_app
from clio_agent.gact.todos import (
    TODO_RECITATION_MARKER,
    TodoError,
    build_write_todos_tool,
    inject_todo_recitation,
    recorded_todos,
)
from clio_agent.gact.types import AgentDef

pytestmark = pytest.mark.usefixtures("host_agent_executor")

_AGENT = AgentDef(id="main", source="expert_pack", title="Main", metadata={})


def _run(app: Any, sid: str, fn: Callable[[], Any], *, thought: str = "") -> Any:
    """Invoke ``fn`` under an active app/session/step context (one ReAct step = one thought)."""

    tok_a = _ctx.set_app(app)
    tok_s = _ctx.set_session_id(sid)
    tok_t = _ctx.set_step_thought(thought)
    try:
        return fn()
    finally:
        _ctx.reset(tok_t)
        _ctx.reset(tok_s)
        _ctx.reset(tok_a)


def _session(app: Any, mode: str = "edit") -> str:
    return app.state.sessions.create(workspace_id="ws_default", title="t", mode=mode).id


# ---- state + whole-list replacement ----------------------------------------------------


def test_write_todos_sets_session_metadata(tmp_path: Path) -> None:
    app = build_app(sessions_path=tmp_path / "s.json")
    sid = _session(app)
    tool = build_write_todos_tool(_AGENT)

    out = _run(app, sid, lambda: tool.func(todos=[{"content": "explore", "status": "pending"}]))

    assert "1 todo" in out
    assert recorded_todos(app.state.sessions.get(sid)) == [
        {"content": "explore", "status": "pending"}
    ]


def test_write_todos_is_whole_list_replacement(tmp_path: Path) -> None:
    """A second write REPLACES the whole list — the first list's items do not linger."""

    app = build_app(sessions_path=tmp_path / "s.json")
    sid = _session(app)
    tool = build_write_todos_tool(_AGENT)

    _run(app, sid, lambda: tool.func(todos=[{"content": "a", "status": "pending"}]), thought="s1")
    _run(
        app,
        sid,
        lambda: tool.func(
            todos=[
                {"content": "b", "status": "in_progress"},
                {"content": "c", "status": "completed"},
            ]
        ),
        thought="s2",
    )

    assert recorded_todos(app.state.sessions.get(sid)) == [
        {"content": "b", "status": "in_progress"},
        {"content": "c", "status": "completed"},
    ]


def test_multiple_in_progress_allowed(tmp_path: Path) -> None:
    """clio runs parallel subagents — multiple in_progress todos are allowed (not rejected)."""

    app = build_app(sessions_path=tmp_path / "s.json")
    sid = _session(app)
    tool = build_write_todos_tool(_AGENT)
    _run(
        app,
        sid,
        lambda: tool.func(
            todos=[
                {"content": "a", "status": "in_progress"},
                {"content": "b", "status": "in_progress"},
            ]
        ),
    )
    assert len(recorded_todos(app.state.sessions.get(sid))) == 2


# ---- parallel / same-step rejection ----------------------------------------------------


def test_two_writes_in_one_step_rejected(tmp_path: Path) -> None:
    """Two whole-list writes in the SAME step are ambiguous — the second is a typed error,
    never a silent merge."""

    app = build_app(sessions_path=tmp_path / "s.json")
    sid = _session(app)
    tool = build_write_todos_tool(_AGENT)

    _run(app, sid, lambda: tool.func(todos=[{"content": "a", "status": "pending"}]), thought="step")
    with pytest.raises(TodoError) as exc:
        _run(
            app, sid, lambda: tool.func(todos=[{"content": "b", "status": "pending"}]), thought="step"
        )
    assert exc.value.reason == "parallel_write"
    # The first write stands; the ambiguous second did not merge/replace.
    assert recorded_todos(app.state.sessions.get(sid)) == [{"content": "a", "status": "pending"}]


def test_consecutive_steps_are_allowed(tmp_path: Path) -> None:
    """A legitimate pending -> in_progress progression across DIFFERENT steps is allowed."""

    app = build_app(sessions_path=tmp_path / "s.json")
    sid = _session(app)
    tool = build_write_todos_tool(_AGENT)
    _run(app, sid, lambda: tool.func(todos=[{"content": "a", "status": "pending"}]), thought="s1")
    _run(app, sid, lambda: tool.func(todos=[{"content": "a", "status": "in_progress"}]), thought="s2")
    assert recorded_todos(app.state.sessions.get(sid)) == [{"content": "a", "status": "in_progress"}]


# ---- validation ------------------------------------------------------------------------


def test_invalid_status_is_typed_error(tmp_path: Path) -> None:
    """'blocked' is NOT a status (blocked != completed): a status outside the enum is rejected."""

    app = build_app(sessions_path=tmp_path / "s.json")
    sid = _session(app)
    tool = build_write_todos_tool(_AGENT)
    with pytest.raises(TodoError) as exc:
        _run(app, sid, lambda: tool.func(todos=[{"content": "x", "status": "blocked"}]))
    assert exc.value.reason == "invalid_status"


def test_empty_content_is_typed_error(tmp_path: Path) -> None:
    app = build_app(sessions_path=tmp_path / "s.json")
    sid = _session(app)
    tool = build_write_todos_tool(_AGENT)
    with pytest.raises(TodoError) as exc:
        _run(app, sid, lambda: tool.func(todos=[{"content": "  ", "status": "pending"}]))
    assert exc.value.reason == "empty_content"


def test_non_list_payload_is_typed_error(tmp_path: Path) -> None:
    app = build_app(sessions_path=tmp_path / "s.json")
    sid = _session(app)
    tool = build_write_todos_tool(_AGENT)
    with pytest.raises(TodoError) as exc:
        _run(app, sid, lambda: tool.func(todos={"content": "x", "status": "pending"}))
    assert exc.value.reason == "invalid_todos"


def test_guidance_present_in_tool_description() -> None:
    """The tool description carries the transition + blocked!=completed + reconcile guidance."""

    tool = build_write_todos_tool(_AGENT)
    desc = tool.desc.lower()
    assert "in_progress" in desc and "pending" in desc and "completed" in desc
    assert "blocked" in desc and "not completed" in desc  # blocked != completed
    assert "reconcile" in desc
    # multiple in_progress explicitly permitted (parallel subagents).
    assert "multiple todos may be in_progress" in desc


# ---- SEPARATION from plan mode ---------------------------------------------------------


def test_write_todos_forbidden_in_plan_mode(tmp_path: Path) -> None:
    """SEPARATION (Codex's rule): write_todos in plan mode is a typed error (plan mode is for
    the plan file, not the checklist)."""

    app = build_app(sessions_path=tmp_path / "s.json")
    sid = _session(app, mode="plan")
    tool = build_write_todos_tool(_AGENT)
    with pytest.raises(TodoError) as exc:
        _run(app, sid, lambda: tool.func(todos=[{"content": "a", "status": "pending"}]))
    assert exc.value.reason == "plan_mode_forbidden"
    assert "not allowed in Plan mode" in str(exc.value)
    # Nothing was recorded.
    assert recorded_todos(app.state.sessions.get(sid)) == []


def test_plan_exit_forbidden_outside_plan_mode(tmp_path: Path) -> None:
    """The mirror separation (regression from P1.4): plan_exit outside plan mode hard-errors."""

    from clio_agent.gact.plan_mode import PlanExitError, build_plan_exit_tool

    app = build_app(sessions_path=tmp_path / "s.json")
    sid = _session(app, mode="edit")
    tool = build_plan_exit_tool(_AGENT)
    with pytest.raises(PlanExitError) as exc:
        _run(app, sid, lambda: tool.func(summary="done"))
    assert "only available in plan mode" in str(exc.value)


# ---- recitation ------------------------------------------------------------------------


def test_recitation_appears_in_execution_mode(tmp_path: Path) -> None:
    app = build_app(sessions_path=tmp_path / "s.json")
    sid = _session(app, mode="edit")
    tool = build_write_todos_tool(_AGENT)
    _run(app, sid, lambda: tool.func(todos=[{"content": "do the thing", "status": "in_progress"}]))

    text = inject_todo_recitation(app, sid, app.state.sessions.get(sid), "USER TURN")
    assert TODO_RECITATION_MARKER in text
    assert "do the thing" in text
    assert "USER TURN" in text


def test_recitation_suppressed_in_plan_mode(tmp_path: Path) -> None:
    """The checklist is NEVER recited in plan mode (plan mode recites the plan file, not todos)."""

    app = build_app(sessions_path=tmp_path / "s.json")
    sid = _session(app, mode="edit")
    tool = build_write_todos_tool(_AGENT)
    _run(app, sid, lambda: tool.func(todos=[{"content": "do the thing", "status": "pending"}]))
    # Flip to plan mode with todos already recorded.
    app.state.sessions.update(sid, mode="plan")

    text = inject_todo_recitation(app, sid, app.state.sessions.get(sid), "USER TURN")
    assert TODO_RECITATION_MARKER not in text
    assert text == "USER TURN"


def test_recitation_noop_when_no_todos(tmp_path: Path) -> None:
    app = build_app(sessions_path=tmp_path / "s.json")
    sid = _session(app, mode="edit")
    assert inject_todo_recitation(app, sid, app.state.sessions.get(sid), "USER TURN") == "USER TURN"
