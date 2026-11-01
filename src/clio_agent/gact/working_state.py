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

__all__ = [
    "PLAN_STALE_SOURCE",
    "WORKING_STATE_SOURCES",
    "note_stale_plan",
    "reinject_after_compaction",
    "working_state_injections",
]

#: Injection sources that are working state: re-injected, never summarized.
WORKING_STATE_SOURCES = frozenset({"plan_mode", "plan", "todos"})
#: The one-line note that an active plan has not been touched for a while.
PLAN_STALE_SOURCE = "plan_stale"
#: Calls that count as keeping the working state current.
_WORKING_STATE_WRITES = frozenset({"plan_update", "write_todos", "plan_enter", "plan_exit"})


def stale_steps() -> int:
    """Steps without plan_update or write_todos before the plan is mentioned (0: never)."""
    from clio_agent import conf  # noqa: PLC0415

    return conf.resolve(
        "plan.stale_steps", env="CLIO_PLAN_STALE_STEPS", default=25, cast=conf.as_int
    )


def note_stale_plan(loop: Any, call_names: list[str]) -> None:
    """Count a recorded step; after ``plan.stale_steps`` quiet ones, say the plan exists.

    A fact, not a decision: the agent judges whether the plan needs updating. The count
    restarts after each note and whenever the step wrote the plan or the todos.
    """
    from clio_agent.gact import context as _ctx  # noqa: PLC0415
    from clio_agent.gact.session_plan import ACTIVE_STATUSES, plan_state  # noqa: PLC0415

    if _WORKING_STATE_WRITES & set(call_names):
        loop._clio_plan_quiet_steps = 0
        return
    quiet = getattr(loop, "_clio_plan_quiet_steps", 0) + 1
    limit = stale_steps()
    loop._clio_plan_quiet_steps = quiet
    if limit <= 0 or quiet < limit:
        return
    app, session_id = _ctx.active_app(), _ctx.active_session_id() or ""
    session = app.state.sessions.get(session_id) if app is not None and session_id else None
    state = plan_state(session) if session is not None else {}
    if state.get("status") not in ACTIVE_STATUSES or not state.get("exists"):
        return
    loop._clio_plan_quiet_steps = 0
    loop.recorder.injections(
        [
            (
                PLAN_STALE_SOURCE,
                f"The plan ({state['path']}) has not been updated in {quiet} steps. If your "
                "understanding or direction has changed, update it with plan_update.",
            )
        ]
    )


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
