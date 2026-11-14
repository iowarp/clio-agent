"""Which open tasks in an assignment's tree still count as its pending work.

An assignment yields instead of completing while its tree holds open work. After a
crash, a call that was in flight is recorded ``task_state_uncertain``: recovery never
resubmits it, and when the assignment that owned it has settled (a researcher that
finished on its other results), nothing can wait on it again. Counting it held every
ancestor open forever, so a flow whose run had ended stayed ``yielded``
(``docs/design/stop-and-restart.md``, durability 4). Such a task stays recorded and
visible with its typed status; it only stops counting as an ancestor's pending work.
"""

from __future__ import annotations

from typing import Any


def orphaned_uncertainty(history: Any, task: Any) -> bool:
    """An uncertain task whose owning assignment has already settled."""

    if (
        task is None
        or task.status != "task_state_uncertain"
        or task.kind in {"Session", "Subagent"}
    ):
        return False
    owner = history.task_snapshot(task.owner.assignment_id) if task.owner.assignment_id else None
    return owner is not None and owner.terminal_at is not None


def pending_work(history: Any, session_id: str, own_handle: str) -> list[Any]:
    """The open tasks under ``session_id`` that the assignment ``own_handle`` waits on."""

    rows = (
        history.task_snapshot(handle)
        for handle in history.tree_handles(session_id, active_only=True)
        if handle != own_handle
    )
    return [row for row in rows if not orphaned_uncertainty(history, row)]


__all__ = ["orphaned_uncertainty", "pending_work"]
