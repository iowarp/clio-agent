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
"""

from __future__ import annotations

import logging
from typing import Any

from clio_agent.gact.human_controls import intervene
from clio_agent.gact.task_access import TaskCaller

logger = logging.getLogger(__name__)

#: Assignment activities that hold a conversation open without running anything.
HELD_ACTIVITIES = frozenset({"stopped", "paused"})
#: Typed reason recorded with each cancellation (no silent settle).
REASON_SESSION_DELETED = "session_deleted_with_held_assignment"


async def settle_held_assignments(app: Any, sid: str) -> list[str]:
    """Cancel the conversation's held assignments before its deletion; returns their handles.

    Nothing happens while a turn runs or when any active task in the tree is not one of
    this conversation's held assignments: the caller's guard then refuses as before.
    """
    history = app.state.task_history
    if app.state.turn_runner.busy(sid):
        return []
    active = history.tree_handles(sid, active_only=True)
    if not active:
        return []

    def held() -> list[str]:
        handles = []
        for handle in sorted(active):
            task = history.task_snapshot(handle)
            if (
                task is None
                or task.kind not in {"Session", "Subagent"}
                or task.owner.session_id != sid
                or task.activity not in HELD_ACTIVITIES
                or task.cancellation_requested_at is not None
            ):
                return []  # other work is active: deletion still waits for it
            handles.append(handle)
        return handles

    handles = await history.io.run(held)
    if not handles:
        return []
    for handle in handles:
        logger.info(
            "Task boundary reason=%s session=%s handle=%s", REASON_SESSION_DELETED, sid, handle
        )
        await intervene(
            app, handle, "cancel", message="The person deleted this stopped conversation."
        )
    caller = TaskCaller("", app.state.human_principal, human_action=True)
    await app.state.task_operations.wait(caller, handles, "all", timeout_s=30)
    return handles


__all__ = ["HELD_ACTIVITIES", "REASON_SESSION_DELETED", "settle_held_assignments"]
