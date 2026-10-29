"""A standing assignment (SPOTTER's watcher) keeps its handle across check segments.

Live defect (cloud qualification, 2026-10-26, ``sess_5f6eea93ae15``): every watcher
check completed its assignment, the completion was published to the protected
session as a ``task_result``, that result admitted a parent turn, and the parent
turn's finalization woke the watcher again: an endless parent/watcher loop.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from clio_agent.arc.memory import ARCMemory
from clio_agent.arc.task_types import AssignmentDetails, MailItem, TaskSnapshot
from clio_agent.gact import conversation_assignments
from clio_agent.gact.conversations import ConversationOutcome, ConversationSegment
from clio_agent.gact.native_work import now
from tests.test_arc.test_conversation_owner_contracts import create_environment


async def _finish_check(
    owner: object, child: TaskSnapshot, outcome: ConversationOutcome
) -> TaskSnapshot:
    def finish() -> TaskSnapshot:
        trigger = MailItem(
            id="wake",
            recipient_session=child.owner.session_id,
            kind="spotter",
            content=owner.history.write_content(
                child.owner.session_id, {"text": "Watched session activity: a tool completed."}
            ),
            created_at=now(),
        )
        segment = ConversationSegment(
            child.owner.session_id, child, "check-turn", trigger, "Watched activity"
        )
        conversation_assignments._finish_segment(owner, segment, outcome)
        return owner.history.task_snapshot(child.handle)

    return await asyncio.to_thread(finish)


@pytest.mark.asyncio
@pytest.mark.parametrize("standing", [True, False])
@pytest.mark.parametrize("requested", [None, "complete"])
async def test_standing_check_yields_without_waking_its_owner(
    arc: ARCMemory, tmp_path: Path, standing: bool, requested: str | None
) -> None:
    app, owner, root = await create_environment(arc, tmp_path)
    try:
        receipt = await owner.spawn(
            owner.caller(root.owner.session_id),
            "Observe the owning session",
            "worker",
            "watch",
            start=False,
            standing=standing,
        )
        child = await asyncio.to_thread(owner.history.task_snapshot, receipt["handle"])
        observed = await _finish_check(
            owner, child, ConversationOutcome({"answer": "Quiet wake."}, disposition=requested)
        )
        parent_mail = await asyncio.to_thread(owner.history.pending_mail, root.owner.session_id)
        if standing:
            assert observed.handle == child.handle and observed.terminal_at is None
            assert observed.status == "active" and observed.activity == "yielded"
            assert [item for item in parent_mail if item.kind == "task_result"] == []
        else:
            # The ordinary child contract is unchanged: it completes and reports back.
            assert observed.status == "completed" and observed.terminal_at is not None
            assert [item.handle for item in parent_mail if item.kind == "task_result"] == [
                child.handle
            ]
    finally:
        await owner.shutdown()
        await owner.recovery.drain()


@pytest.mark.asyncio
async def test_failed_standing_check_settles_and_its_successor_stays_standing(
    arc: ARCMemory, tmp_path: Path
) -> None:
    """A failed check still settles (clearance fails closed on it); a successor inherits."""
    app, owner, root = await create_environment(arc, tmp_path)
    try:
        receipt = await owner.spawn(
            owner.caller(root.owner.session_id),
            "Observe the owning session",
            "worker",
            "watch",
            start=False,
            standing=True,
        )
        child = await asyncio.to_thread(owner.history.task_snapshot, receipt["handle"])
        failed = await _finish_check(
            owner,
            child,
            ConversationOutcome({}, error={"error": "provider_error", "message": "Recorded"}),
        )
        assert failed.status == "failed" and failed.terminal_at is not None

        def successor() -> AssignmentDetails:
            task = owner._new_assignment(
                child.owner.session_id,
                "Observe the owning session",
                "human",
                predecessor=failed,
                invocation_id="watch-successor",
            )
            return AssignmentDetails.model_validate(owner.history.read_content(task.details))

        assert (await asyncio.to_thread(successor)).standing is True
    finally:
        await owner.shutdown()
        await owner.recovery.drain()
