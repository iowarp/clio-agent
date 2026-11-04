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
    "SESSION_FACTS_SOURCE",
    "WORKING_STATE_SOURCES",
    "note_stale_plan",
    "offer_person_edit",
    "reinject_after_compaction",
    "working_state_injections",
]

#: Injection sources that are working state: re-injected, never summarized.
WORKING_STATE_SOURCES = frozenset({"session_facts", "plan_mode", "plan", "todos"})
#: The session's own facts (workspace root, write roots, machine, scratch folder). They
#: ride the conversation, not the system prompt: the system prompt is cached as one block,
#: and per-session text in it made every session write the whole prompt to the cache
#: again (final evaluation, 2026-11-02: about 36k cache-creation tokens per run).
SESSION_FACTS_SOURCE = "session_facts"
#: The one-line note that an active plan has not been touched for a while.
PLAN_STALE_SOURCE = "plan_stale"
#: Calls that count as keeping the working state current.
_WORKING_STATE_WRITES = frozenset({"plan_update", "write_todos", "plan_enter", "plan_exit"})


def stale_steps() -> int:
    """Steps without plan_update or write_todos before the plan is mentioned (0: never):
    the ``steps`` setting of the reminder ``clio.reminder.plan_stale``.

    Only steps that call tools count, across the session's turns. 8 was tried on L1P
    (nine short rounds, about 18 tool steps), where it kept plans current in 4 of 4 runs,
    but a plan that is still right needs no update and the note cannot tell; the owner
    chose 25, so it fires a few times at most in a long multi-turn session."""
    from clio_agent import injections  # noqa: PLC0415

    return injections.param(PLAN_STALE_SOURCE, "steps", 25, int)


def _quiet_steps(app: Any, loop: Any) -> dict[str, int]:
    """The session's quiet-step counts, kept on the app so they run across turns.

    A session of many short turns (a person asking one thing at a time) would otherwise
    restart the count at every turn and never be told. In memory only: a server restart
    restarts the count, which costs at most one later note.
    """
    if app is None:
        counts = getattr(loop, "_clio_plan_quiet_steps", None)
        if counts is None:
            counts = loop._clio_plan_quiet_steps = {}
        return counts
    counts = getattr(app.state, "plan_quiet_steps", None)
    if counts is None:
        counts = app.state.plan_quiet_steps = {}
    return counts


def note_stale_plan(loop: Any, call_names: list[str]) -> None:
    """Count a recorded step; after the reminder's ``steps`` quiet ones, say the plan exists.

    A fact, not a decision: the agent judges whether the plan needs updating. The count
    runs across the session's turns and restarts after each note and whenever the step
    wrote the plan or the todos.
    """
    from clio_agent.gact import context as _ctx  # noqa: PLC0415
    from clio_agent.gact.session_plan import ACTIVE_STATUSES, plan_state  # noqa: PLC0415

    app, session_id = _ctx.active_app(), _ctx.active_session_id() or ""
    counts = _quiet_steps(app, loop)
    if _WORKING_STATE_WRITES & set(call_names):
        counts[session_id] = 0
        return
    from clio_agent import injections  # noqa: PLC0415

    quiet = counts.get(session_id, 0) + 1
    limit = getattr(loop, "_clio_plan_stale_limit", None)
    if limit is None:  # read once per turn: the loop lives for one turn
        limit = loop._clio_plan_stale_limit = stale_steps()
    counts[session_id] = quiet
    if limit <= 0 or quiet < limit:
        return
    session = app.state.sessions.get(session_id) if app is not None and session_id else None
    state = plan_state(session) if session is not None else {}
    if state.get("status") not in ACTIVE_STATUSES or not state.get("exists"):
        return
    counts[session_id] = 0
    text = injections.render(PLAN_STALE_SOURCE, {"path": state["path"], "steps": quiet})
    if text:
        loop.recorder.injections([(PLAN_STALE_SOURCE, text)])


def working_state_injections(
    app: Any, session_id: str, session: Any, *, force_full: bool = False
) -> list[tuple[str, str]]:
    """``(source, text)`` for the session's facts, the plan-mode reminder, the plan and the
    todos (non-empty)."""
    from clio_agent.gact.agents.composition import (  # noqa: PLC0415
        _runtime_active_workspace_context,
    )
    from clio_agent.gact.plan_mode import plan_mode_reminder  # noqa: PLC0415
    from clio_agent.gact.session_plan import (  # noqa: PLC0415
        PLAN_SOURCE,
        plan_injection,
        take_person_edit,  # noqa: PLC0415
    )
    from clio_agent.gact.todos import todo_recitation  # noqa: PLC0415

    plan = take_person_edit(app, session_id) or plan_injection(app, session_id, session)
    blocks = (
        (SESSION_FACTS_SOURCE, _runtime_active_workspace_context(app, session_id=session_id)),
        ("plan_mode", plan_mode_reminder(app, session_id, session, force_full=force_full)),
        (PLAN_SOURCE, plan),
        ("todos", todo_recitation(app, session_id, session)),
    )
    return [(source, text) for source, text in blocks if text]


def offer_person_edit(recorder: Any) -> None:
    """Before a step: a plan the person just edited reaches the agent now, mid-turn too."""
    from clio_agent.gact import context as _ctx  # noqa: PLC0415
    from clio_agent.gact.session_plan import PLAN_SOURCE, take_person_edit  # noqa: PLC0415

    app, session_id = _ctx.active_app(), _ctx.active_session_id() or ""
    if app is None or not session_id:
        return
    text = take_person_edit(app, session_id)
    if text:
        recorder.injections([(PLAN_SOURCE, text)])


def reinject_after_compaction(recorder: Any) -> None:
    """A compaction just ran in this turn: offer the working state again (unless
    ``compaction.keep.working_state`` is off, to measure what the re-offer is worth)."""
    from clio_agent.gact import context as _ctx  # noqa: PLC0415
    from clio_agent.gact.compaction_policy import keep_policy  # noqa: PLC0415

    if not keep_policy().keep_working_state:
        return

    app, session_id = _ctx.active_app(), _ctx.active_session_id() or ""
    session = app.state.sessions.get(session_id) if app is not None and session_id else None
    if session is None:
        return
    recorder.injections(working_state_injections(app, session_id, session, force_full=True))
