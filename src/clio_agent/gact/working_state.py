"""The working state CLIO offers back to the agent: the plan, plan mode and the todos.

Offered at every turn start (the turn-start injections) and right after any compaction,
including one in the middle of a turn (:func:`reinject_after_compaction`, called by the
agent loop when a compaction ran). The recorder records an injection only when it is
absent from the working set or changed, so after a compaction folds the old copies away
they come back verbatim. Compaction leaves these injections out of what it summarizes
(:data:`WORKING_STATE_SOURCES`), and the compaction prompt is told they return.
"""

from __future__ import annotations

from typing import Any

__all__ = ["WORKING_STATE_SOURCES", "reinject_after_compaction", "working_state_injections"]

#: Injection sources that are working state: re-injected, never summarized.
WORKING_STATE_SOURCES = frozenset({"plan_mode", "plan", "todos"})


def working_state_injections(
    app: Any, session_id: str, session: Any, *, force_full: bool = False
) -> list[tuple[str, str]]:
    """``(source, text)`` for the plan-mode reminder, the plan and the todos (non-empty)."""
    from clio_agent.gact.plan_mode import plan_mode_reminder  # noqa: PLC0415
    from clio_agent.gact.session_plan import PLAN_SOURCE, plan_injection  # noqa: PLC0415
    from clio_agent.gact.todos import todo_recitation  # noqa: PLC0415

    blocks = (
        ("plan_mode", plan_mode_reminder(app, session_id, session, force_full=force_full)),
        (PLAN_SOURCE, plan_injection(app, session_id, session)),
        ("todos", todo_recitation(app, session_id, session)),
    )
    return [(source, text) for source, text in blocks if text]


def reinject_after_compaction(recorder: Any) -> None:
    """A compaction just ran in this turn: offer the working state again."""
    from clio_agent.gact import context as _ctx  # noqa: PLC0415

    app, session_id = _ctx.active_app(), _ctx.active_session_id() or ""
    session = app.state.sessions.get(session_id) if app is not None and session_id else None
    if session is None:
        return
    recorder.injections(working_state_injections(app, session_id, session, force_full=True))
