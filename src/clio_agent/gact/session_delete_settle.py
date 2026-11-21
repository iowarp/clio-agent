"""Deleting a stopped conversation settles the assignment Stop held for it (owner module).

Stop ends the turn but keeps the conversation's assignment open (activity ``stopped``,
or ``paused`` under a model hold), so the person's next message can continue it. That
held assignment is not running work, yet it is a non-terminal task in the
conversation's tree, so ``DELETE /v1/sessions/{sid}`` refused every stopped
conversation with ``session_has_active_work``. In the UI that read "Request failed
with 409 Conflict" with no way forward: pressing Stop made a conversation undeletable.

Deleting is the person's decision to discard the conversation, so delete cancels those
held assignments through the human task controls, attributed and on the record, and
waits for their settlement. It does so only when they are the only active work in the
tree and no turn is running: accepted work that still runs keeps the 409.

A call a restart cut off is not reachable either. Its stdio server ended with the
restart, so it is recorded ``task_state_uncertain`` and no live connection carries it:
cancel answers "Original task transport is unavailable" and it stays active for ever.
Stop and restart already exempt such a call (:func:`uncertain_call`), but it kept the
conversation undeletable. Delete ends it ``interrupted`` with ``settlement: unknown``
and a typed reason saying its work may still run where it was started.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import uuid4

from clio_agent.arc.task_types import TaskProgress
from clio_agent.gact.assignment_pending import uncertain_call
from clio_agent.gact.external_task_inputs import finish_terminal
from clio_agent.gact.human_controls import intervene
from clio_agent.gact.native_work import now
from clio_agent.gact.task_access import TaskCaller

logger = logging.getLogger(__name__)

#: Assignment activities that hold a conversation open without running anything.
HELD_ACTIVITIES = frozenset({"stopped", "paused"})
#: Typed reason recorded with each cancellation (no silent settle).
REASON_SESSION_DELETED = "session_deleted_with_held_assignment"
#: Typed reason recorded when delete ends an external task no connection carries.
REASON_UNREACHABLE = "session_deleted_with_unreachable_task"


def _is_held(task: Any, sid: str) -> bool:
    return (
        task.kind in {"Session", "Subagent"}
        and task.owner.session_id == sid
        and task.activity in HELD_ACTIVITIES
        and task.cancellation_requested_at is None
    )


def _bound_handles(app: Any) -> set[str]:
    """Handles of the external tasks a live connection still carries."""
    external = getattr(app.state, "external_tasks", None)
    connections = getattr(external, "connections", {}) or {}
    return {
        call.submission.handle
        for host in tuple(connections.values())
        for call in tuple(host.calls.values())
    }


def _is_unreachable(task: Any, sid: str, bound: set[str]) -> bool:
    """A call cut off and left uncertain (as Stop and restart exempt it), unbound now."""
    return uncertain_call(task) and task.owner.session_id == sid and task.handle not in bound


def _end_unreachable(history: Any, handle: str) -> bool:
    """Record the lost task as interrupted, outcome unknown; whether this ended it."""
    with history.serialized():
        current = history.task_snapshot(handle)
        if current is None or current.terminal_at is not None:
            return False
        stamp = now()
        message = (
            f"{REASON_UNREACHABLE}: the person deleted this conversation. Its connection "
            f"to {current.tool or 'the server'} was lost, so nothing here could cancel it; "
            "the work it started may still run where it was started."
        )
        history.record_snapshot(
            current.model_copy(
                update={
                    "status": "interrupted",
                    "raw_status": REASON_UNREACHABLE,
                    "activity": "",
                    "settlement": "unknown",
                    "terminal_at": stamp,
                    "updated_at": stamp,
                    "progress": TaskProgress(message=message),
                    "supported_actions": (),
                }
            ),
            observation_id=uuid4().hex,
        )
    return True


async def settle_held_assignments(app: Any, sid: str) -> list[str]:
    """Settle what holds the conversation without running before its deletion.

    That is its held assignments, which are cancelled, and its external tasks that no
    live connection carries, which end interrupted. Returns their handles. Nothing
    happens while a turn runs or when any other task in the tree is active: the
    caller's guard then refuses as before.
    """
    history = app.state.task_history
    if app.state.turn_runner.busy(sid):
        return []
    active = history.tree_handles(sid, active_only=True)
    if not active:
        return []
    bound = _bound_handles(app)

    def classify() -> tuple[list[str], list[str]]:
        held, unreachable = [], []
        for handle in sorted(active):
            task = history.task_snapshot(handle)
            if task is None:
                return [], []
            if _is_held(task, sid):
                held.append(handle)
            elif _is_unreachable(task, sid, bound):
                unreachable.append(handle)
            else:
                return [], []  # other work is active: deletion still waits for it
        return held, unreachable

    held, unreachable = await history.io.run(classify)
    for handle in unreachable:
        logger.info("Task boundary reason=%s session=%s handle=%s", REASON_UNREACHABLE, sid, handle)
        if await history.io.run(_end_unreachable, history, handle):
            external = getattr(app.state, "external_tasks", None)
            if external is not None:  # its notice, open questions and obligation
                await history.io.run(finish_terminal, external, handle)
    for handle in held:
        logger.info(
            "Task boundary reason=%s session=%s handle=%s", REASON_SESSION_DELETED, sid, handle
        )
        await intervene(
            app, handle, "cancel", message="The person deleted this stopped conversation."
        )
    if held:
        caller = TaskCaller("", app.state.human_principal, human_action=True)
        await app.state.task_operations.wait(caller, held, "all", timeout_s=30)
    return held + unreachable


__all__ = [
    "HELD_ACTIVITIES",
    "REASON_SESSION_DELETED",
    "REASON_UNREACHABLE",
    "settle_held_assignments",
]
