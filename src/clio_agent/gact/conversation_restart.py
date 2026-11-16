"""Restart reconciliation for terminal parents with retained descendant work."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING
from uuid import uuid4

from clio_agent.arc.task_types import MailItem, TaskSnapshot, argument_digest
from clio_agent.gact import task_history_queries
from clio_agent.gact.native_work import now
from clio_agent.gact.task_access import TaskAccess, TaskCaller
from clio_agent.gact.task_publication import publish_control_outcome

if TYPE_CHECKING:
    from clio_agent.gact.conversations import Conversations
    from clio_agent.gact.task_history import TaskHistory

logger = logging.getLogger(__name__)


async def restart(self: Conversations, caller: TaskCaller, handle: str) -> dict[str, object]:
    """Request a linked assignment after known cancellation/worker settlement."""
    current = await self.history.io.run(TaskAccess(self.history, caller).require, handle, "control")
    self.require_execution_owner(current)
    if current.kind not in {"Session", "Subagent"}:
        raise ValueError("Only conversation assignments restart")
    if handle in self._restarting:
        return {"handle": handle, "restart_requested": True, "pending": True}
    latest = await self.history.io.run(self.history.latest_assignment, current.owner.session_id)
    if latest is None or latest.handle != handle:
        raise ValueError("Historical assignment already has a successor")
    if handle in self._restarting:
        return {"handle": handle, "restart_requested": True, "pending": True}
    self._restarting.add(handle)
    interrupted: dict[str, object] = {}
    try:
        if current.terminal_at is None:
            interrupted = await self.history.io.run(_running_work, self.history, current)
            await self.cancel(caller, handle)
        cancellation, verify_descendants = await prepare_terminal_restart(self, current)
    except Exception:
        self._restarting.discard(handle)
        raise

    async def restart_settled() -> None:
        if cancellation is not None:
            # Unknown settlement ends this restart request; it never launches later.
            if current.terminal_at is None:
                await self.app.state.task_operations.wait(
                    TaskCaller("", current.owner.human_id, human_action=True), handle
                )
            settled = await self.history.io.run(self.history.task_snapshot, handle)
            if (
                settled is None
                or settled.terminal_at is None
                or settled.settlement not in {"confirmed", "never_started"}
            ):
                raise ValueError(
                    "Restart settlement remains uncertain; no successor launched. "
                    + (
                        settled.progress.message
                        if settled is not None
                        else "Assignment unavailable"
                    )
                )
            await asyncio.shield(cancellation)
            if verify_descendants is not None:
                await verify_descendants()

        def create() -> None:
            with self.history.serialized():
                previous = TaskAccess(self.history, caller).require(handle, "control")
                if previous.terminal_at is None or previous.settlement not in {
                    "confirmed",
                    "never_started",
                }:
                    raise ValueError("Restart settlement remains uncertain; no successor launched")
                latest = self.history.latest_assignment(previous.owner.session_id)
                if latest is None or latest.handle != previous.handle:
                    raise ValueError("Assignment acquired a successor while restart waited")
                resume_stopped_parent(self, previous)
                task = self._new_assignment(
                    previous.owner.session_id,
                    previous.description or "Retained assignment",
                    previous.owner.human_id,
                    predecessor=previous,
                    invocation_id="restart:" + uuid4().hex,
                )
                content = self.history.write_content(
                    task.owner.session_id,
                    "This is a linked restart with retained Core context. Inspect the last confirmed operations and task handles; effects may have happened before a result was recorded. Do not replay accepted operations automatically.",
                )
                self.history.record_mail(
                    MailItem(
                        id="restart:" + task.handle,
                        recipient_session=task.owner.session_id,
                        kind="control",
                        content=content,
                        created_at=now(),
                        handle=task.handle,
                    )
                )
                self._control_holds.discard(previous.handle)
                publish_control_outcome(
                    self.history,
                    caller,
                    previous,
                    "restart",
                    {
                        "status": "started",
                        "successor_handle": task.handle,
                        "session_id": task.owner.session_id,
                    },
                    "restart-outcome:" + task.handle,
                )

        await self.history.io.run(create)

    async def restart_once() -> None:
        try:
            await restart_settled()
        except Exception as error:
            reason = str(error)

            async def report() -> None:
                await self.history.io.run(
                    publish_control_outcome,
                    self.history,
                    caller,
                    current,
                    "restart",
                    {"status": "not_started", "error": reason},
                    "restart-attention:" + handle + ":" + argument_digest({"reason": reason}),
                )

            try:
                await report()
            except Exception as error:
                logger.exception(
                    "Task boundary reason=restart_attention_recording_unconfirmed error_type=%s",
                    type(error).__name__,
                )
                self.recovery.defer("restart-attention:" + handle, report)
            raise
        finally:
            self._restarting.discard(handle)

    self.retain_control("restart:" + handle, asyncio.create_task(restart_once()))
    return {"handle": handle, "restart_requested": True, "pending": True, **interrupted}


def _running_work(history: TaskHistory, assignment: TaskSnapshot) -> dict[str, object]:
    """What restarting a still-running assignment cancels, told to whoever asked.

    A running target (one the service resumed after its own restart is running) is
    cancelled before its successor starts. The answer says so and names the open work
    it ends, so the caller does not mistake the loss for something else.
    """

    rows = []
    with history.read_view():
        for member in history.tree_handles(assignment.owner.session_id):
            task = history.task_snapshot(member)
            if (
                task is None
                or task.terminal_at is not None
                or task.handle == assignment.handle
                or task.owner.parent_handle != assignment.handle
            ):
                continue
            rows.append({"handle": task.handle, "kind": task.kind, "label": task.label})
    names = ", ".join(row["label"] or row["handle"] for row in rows)
    return {
        "was_running": True,
        "cancelled_unfinished": rows,
        "note": (
            "This task was still running when the restart was asked for, so the restart "
            "cancelled its unfinished work first"
            + (f" ({names})" if names else "")
            + ". Finished calls keep their results; the cancelled work runs again in the "
            "successor. To keep work in progress, observe or wait on a running task instead "
            "of restarting it."
        ),
    }


async def prepare_terminal_restart(
    owner: Conversations, assignment: TaskSnapshot
) -> tuple[asyncio.Task[None] | None, Callable[[], Awaitable[None]] | None]:
    """Cancel and verify only confirmed descendants of an already terminal assignment."""
    handle = assignment.handle
    cancellation = owner._controls.get(handle)
    if assignment.terminal_at is None:
        return cancellation, None

    owner._control_holds.add(handle)
    descendants_index = await owner.history.io.run(
        task_history_queries.descendant_handles, owner.history, handle
    )
    descendants = tuple(sorted(descendants_index - {handle}))
    if not descendants:
        owner._control_holds.discard(handle)
        return cancellation, None

    from clio_agent.gact.assignment_pending import uncertain_call

    def already_cut_off() -> frozenset[str]:
        return frozenset(
            member for member in descendants if uncertain_call(owner.history.task_snapshot(member))
        )

    # A call an earlier crash or restart cut off (uncertain, never resubmitted) has no
    # running work to settle; it stays recorded and does not block the restart (as for a
    # stop). A call this restart's own cancellation leaves uncertain may still be running.
    cut_off = await owner.history.io.run(already_cut_off)

    def settled() -> bool:
        from clio_agent.gact.conversation_controls import _member_settled

        return all(
            member in cut_off or _member_settled(owner.history.task_snapshot(member))
            for member in descendants
        )

    if await owner.history.io.run(settled):
        owner._control_holds.discard(handle)
        return cancellation, None

    if cancellation is None or cancellation.done():
        from clio_agent.gact.conversation_controls import _cancel_subtree

        cancellation = asyncio.create_task(_cancel_subtree(owner, assignment, members=descendants))
        owner.retain_control(handle, cancellation)

    async def verify_settled() -> None:
        if not await owner.history.io.run(settled):
            raise ValueError(
                "Restart descendant settlement remains unconfirmed; no successor launched."
            )

    return cancellation, verify_settled


def resume_stopped_parent(owner: Conversations, child: TaskSnapshot) -> TaskSnapshot | None:
    """Give a child restarted after its conversation was stopped a live conversation.

    Stop with ``children: stop`` cancels the conversation's assignment, which closes
    admission for everything under it. Restarting one of its children then needs a
    parent that can receive the result: the stopped conversation gets a linked
    successor assignment (``restart-child:<handle>``). The successor sends no input
    of its own; the child's result wakes it, as any task result does. Ancestors
    stopped the same way are resumed first. Called under ``history.serialized()``.
    """

    if not child.owner.parent_handle:
        return None
    parent = owner.history.task_snapshot(child.owner.parent_handle)
    if parent is None:
        return None
    latest = owner.history.latest_assignment(parent.owner.session_id)
    if (
        latest is None
        or latest.terminal_at is None
        or latest.cancellation_requested_at is None
        or latest.owner.human_id != child.owner.human_id
        or not owner.supports_execution(latest)
    ):
        return None
    resume_stopped_parent(owner, latest)
    return owner._new_assignment(
        latest.owner.session_id,
        latest.description or "Retained assignment",
        latest.owner.human_id,
        predecessor=latest,
        invocation_id="restart-child:" + child.handle,
    )
