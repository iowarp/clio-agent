"""Exact invocation fences for accepted flow calls whose receipt raced an error."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from clio_agent.gact.task_access import TaskCaller
from clio_agent.gact.task_history_queries import direct_child_by_invocation

if TYPE_CHECKING:
    from clio_agent.gact.conversations import Conversations

logger = logging.getLogger(__name__)


def close_spawn_invocations(
    owner: Conversations,
    parent_handle: str,
    invocation_ids: tuple[str, ...],
) -> dict[str, tuple[str, ...]] | None:
    """Fence exact not-yet-accepted calls and return already committed child handles.

    Runs on CoreIO's serialized queue after any earlier create transaction. It
    schedules ordinary owner cancellation so a caller timeout cannot orphan a
    commit that was queued ahead of this fence.
    """
    owner._closed_spawn_invocations.update(
        (parent_handle, invocation_id) for invocation_id in invocation_ids
    )
    root = owner.history.task_snapshot(parent_handle)
    if root is None:
        return None
    matches: dict[str, tuple[str, ...]] = {}
    for invocation_id in invocation_ids:
        rows = direct_child_by_invocation(
            owner.history,
            parent_handle,
            invocation_id,
            session_id=root.owner.session_id,
            human_id=root.owner.human_id,
        )
        handles = tuple(row.handle for row in rows)
        matches[invocation_id] = handles
        for handle in handles:
            schedule_spawn_cleanup(owner, parent_handle, handle)
    return matches


def release_spawn_invocations(
    owner: Conversations, parent_handle: str, invocation_ids: tuple[str, ...]
) -> None:
    """Retire fences after the associated dispatch coroutines have ended."""
    owner._closed_spawn_invocations.difference_update(
        (parent_handle, invocation_id) for invocation_id in invocation_ids
    )


def schedule_spawn_cleanup(owner: Conversations, parent_handle: str, handle: str) -> None:
    """Request normal owner cancellation for a child accepted after its caller timed out."""
    parent = owner.history.task_snapshot(parent_handle)
    loop = owner._loop
    if parent is None or loop is None or loop.is_closed():
        return
    caller = TaskCaller(parent.owner.session_id, parent.owner.human_id)

    async def request() -> None:
        try:
            await owner.cancel(caller, handle)
        except Exception:
            logger.exception("late flow child cleanup could not be requested handle=%s", handle)

    def schedule() -> None:
        if not owner._closing:
            task = asyncio.create_task(request())
            owner.retain_control("flow-child-cleanup:" + handle, task)

    loop.call_soon_threadsafe(schedule)
