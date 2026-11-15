"""Which open tasks in an assignment's tree still count as its pending work.

An assignment yields instead of completing while its tree holds open work. After a
crash, a call that was in flight is recorded ``task_state_uncertain``: recovery never
resubmits it, and when the assignment that owned it has settled (a researcher that
finished on its other results), nothing can wait on it again. Counting it held every
ancestor open forever, so a flow whose run had ended stayed ``yielded``
(``docs/design/stop-and-restart.md``, durability 4). Such a task stays recorded and
visible with its typed status; it only stops counting as an ancestor's pending work.

Nor does it count for the assignment that owns it, when that assignment's own turn
ends: the turn was told the call's outcome is uncertain (an attention notice), waits
treat it as settled, and nothing will move it again. Counting it held a researcher
that had answered ``yielded`` forever, and its flow with it, whenever the model ended
its turn without an explicit disposition. A turn that does mean to wait still says so
with ``assignment_disposition="yield"``.
"""

from __future__ import annotations

from typing import Any


def orphaned_uncertainty(history: Any, task: Any, settling: str = "") -> bool:
    """An uncertain call whose owning assignment has settled, or is ``settling`` now."""

    if (
        task is None
        or task.status != "task_state_uncertain"
        or task.kind in {"Session", "Subagent"}
    ):
        return False
    if settling and task.owner.assignment_id == settling:
        return True
    owner = history.task_snapshot(task.owner.assignment_id) if task.owner.assignment_id else None
    return owner is not None and owner.terminal_at is not None


def pending_work(history: Any, session_id: str, own_handle: str) -> list[Any]:
    """The open tasks under ``session_id`` that the assignment ``own_handle`` waits on."""

    rows = (
        history.task_snapshot(handle)
        for handle in history.tree_handles(session_id, active_only=True)
        if handle != own_handle
    )
    return [row for row in rows if not orphaned_uncertainty(history, row, own_handle)]


__all__ = ["orphaned_uncertainty", "pending_work"]
