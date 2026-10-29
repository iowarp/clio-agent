"""Mail-aware waits on real Core preserve pending tasks and input commitment."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest

from clio_agent.arc.memory import ARCMemory
from clio_agent.arc.task_types import (
    AssignmentDetails,
    ContextBoundary,
    CoordinationGrant,
    TaskOwner,
    TaskSnapshot,
)
from clio_agent.gact.session_mail import SessionMail
from clio_agent.gact.task_access import TaskCaller
from clio_agent.gact.task_history import TaskHistory
from clio_agent.gact.task_operations import TaskOperations
from tests.test_arc.test_task_replacement_contracts import STAMP, finish, forbid_cancel, record_task


@dataclass
class World:
    """Actual history, mailbox and wait owner with passive test work observations."""

    history: TaskHistory
    mail: SessionMail
    operations: TaskOperations
    caller: TaskCaller
    tasks: tuple[TaskSnapshot, TaskSnapshot]
    grant: CoordinationGrant


def world(arc: ARCMemory) -> World:
    """Use real Core authorization and durable mail; start no model workers."""
    history = TaskHistory(arc)
    assignment = TaskSnapshot(
        handle="task_sender",
        kind="Session",
        tool="conversation",
        label="Sender",
        invocation_id="fixture:sender",
        owner=TaskOwner(human_id="human", session_id="sender", assignment_id="task_sender"),
        created_at=STAMP,
        updated_at=STAMP,
        status="active",
        raw_status="active",
        details=history.write_content("sender", AssignmentDetails().model_dump(mode="json")),
    )
    history.record_snapshot(assignment, observation_id="sender")
    grant = CoordinationGrant(
        id="sender-parent",
        granting_human="human",
        recipient_session="sender",
        target_session="parent",
        rights=("message",),
        created_at=STAMP,
    )
    history.record_grant(grant, observation_id="grant")
    mail = SessionMail(history, lambda _sid, _item: True)
    app = SimpleNamespace(
        state=SimpleNamespace(
            conversations=SimpleNamespace(
                mail=mail,
                cancellation_job=lambda _handle: None,
            )
        )
    )
    tasks = tuple(record_task(history, handle, session="parent") for handle in ("task_a", "task_b"))
    return World(
        history,
        mail,
        TaskOperations(history, forbid_cancel, app=app),
        TaskCaller("parent", "human"),
        (tasks[0], tasks[1]),
        grant,
    )


async def send(value: World, text: str = "Configuration is ready") -> Any:
    """Accept an authorized peer message through the existing mailbox owner."""
    return await asyncio.to_thread(value.mail.peer, TaskCaller("sender", "human"), "parent", text)


@pytest.mark.asyncio
@pytest.mark.parametrize("early", [True, False])
@pytest.mark.parametrize("policy", ["all", "any"])
async def test_addressed_mail_interrupts_without_consuming_or_settling(
    arc: ARCMemory,
    early: bool,
    policy: str,
) -> None:
    value = await asyncio.to_thread(world, arc)
    item = await send(value) if early else None
    waiter = asyncio.create_task(
        value.operations.wait(
            value.caller,
            [task.handle for task in value.tasks],
            return_when=policy,
            interrupt_on_mail=True,
        )
    )
    try:
        if item is None:
            item = await send(value)
        result = await asyncio.wait_for(waiter, 10)
        assert result["interrupted"] == "mail_received" and result["timed_out"] is False
        assert result["outcomes"] == []
        assert set(result["pending"]) == {task.handle for task in value.tasks}
        assert await asyncio.to_thread(value.mail.interrupting_input, value.caller) == (item.id,)
        for task in value.tasks:
            current = await asyncio.to_thread(value.history.task_snapshot, task.handle)
            assert current is not None and current.terminal_at is None
            assert current.cancellation_requested_at is None
    finally:
        waiter.cancel()
        await asyncio.gather(waiter, return_exceptions=True)


@pytest.mark.asyncio
async def test_outcome_only_wait_ignores_mail_and_collects_both_actual_results(
    arc: ARCMemory,
) -> None:
    value = await asyncio.to_thread(world, arc)
    await send(value)
    waiter = asyncio.create_task(
        value.operations.wait(value.caller, [t.handle for t in value.tasks])
    )
    try:
        await asyncio.to_thread(finish, value.history, value.tasks[0])
        await asyncio.sleep(0)
        assert not waiter.done()
        await asyncio.to_thread(finish, value.history, value.tasks[1])
        result = await asyncio.wait_for(waiter, 10)
        assert result["pending"] == [] and "interrupted" not in result
        assert len(result["outcomes"]) == 2
    finally:
        waiter.cancel()
        await asyncio.gather(waiter, return_exceptions=True)


@pytest.mark.asyncio
async def test_partial_completion_notice_does_not_interrupt_all_but_peer_mail_does(
    arc: ARCMemory,
) -> None:
    value = await asyncio.to_thread(world, arc)
    await asyncio.to_thread(finish, value.history, value.tasks[0])
    assert await asyncio.to_thread(value.mail.interrupting_input, value.caller) == ()
    waiter = asyncio.create_task(
        value.operations.wait(
            value.caller,
            [t.handle for t in value.tasks],
            interrupt_on_mail=True,
        )
    )
    try:
        await send(value)
        result = await asyncio.wait_for(waiter, 10)
        assert result["interrupted"] == "mail_received"
        assert result["pending"] == [value.tasks[1].handle]
        assert result["outcomes"][0]["status"] == "completed"
    finally:
        waiter.cancel()
        await asyncio.gather(waiter, return_exceptions=True)


@pytest.mark.asyncio
async def test_coalesced_mail_commits_once_and_does_not_reinterrupt(arc: ARCMemory) -> None:
    value = await asyncio.to_thread(world, arc)
    first, second = await send(value, "first"), await send(value, "second")
    result = await value.operations.wait(
        value.caller, value.tasks[0].handle, interrupt_on_mail=True
    )
    assert result["interrupted"] == "mail_received"
    staged = await asyncio.to_thread(value.mail.stage, value.caller)
    assert tuple(item.id for item in staged.items) == (first.id, second.id)
    await asyncio.to_thread(
        value.mail.commit,
        value.caller,
        staged,
        assignment_id="parent-assignment",
        iteration_id="model-step",
        boundary=ContextBoundary(session_id="parent", scope="main", logical_time=0),
    )
    assert await asyncio.to_thread(value.mail.interrupting_input, value.caller) == ()
    waiter = asyncio.create_task(
        value.operations.wait(
            value.caller,
            value.tasks[0].handle,
            interrupt_on_mail=True,
        )
    )
    try:
        third = await send(value, "new coordination")
        assert (await asyncio.wait_for(waiter, 10))["interrupted"] == "mail_received"
        assert await asyncio.to_thread(value.mail.interrupting_input, value.caller) == (third.id,)
    finally:
        waiter.cancel()
        await asyncio.gather(waiter, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("blocked", ["revoked", "queued", "held", "ineligible"])
async def test_unavailable_mail_does_not_interrupt(arc: ARCMemory, blocked: str) -> None:
    value = await asyncio.to_thread(world, arc)
    item = await send(value)
    if blocked == "revoked":
        await asyncio.to_thread(
            value.history.record_grant,
            value.grant.model_copy(update={"revoked_at": STAMP}),
            observation_id="revoke",
        )
    elif blocked == "ineligible":
        value.mail.eligible = lambda _sid, _item: False
    else:
        await asyncio.to_thread(
            value.history.update_mail,
            item.model_copy(update={"delivery": blocked, "revision": 2}),
            expected_revision=1,
        )
    assert await asyncio.to_thread(value.mail.interrupting_input, value.caller) == ()
    result = await value.operations.wait(
        value.caller, value.tasks[0].handle, timeout_s=0, interrupt_on_mail=True
    )
    assert result["timed_out"] and "interrupted" not in result


@pytest.mark.asyncio
async def test_other_recipient_and_failed_send_cannot_interrupt(arc: ARCMemory) -> None:
    value = await asyncio.to_thread(world, arc)
    await asyncio.to_thread(
        value.history.record_grant,
        value.grant.model_copy(update={"id": "sender-other", "target_session": "other"}),
        observation_id="other-grant",
    )
    await asyncio.to_thread(value.mail.peer, TaskCaller("sender", "human"), "other", "unrelated")
    with pytest.raises(ValueError, match="unavailable"):
        await asyncio.to_thread(value.mail.peer, TaskCaller("sender", "human"), "unknown", "denied")
    assert await asyncio.to_thread(value.mail.interrupting_input, value.caller) == ()
    waiter = asyncio.create_task(
        value.operations.wait(
            value.caller,
            value.tasks[0].handle,
            interrupt_on_mail=True,
        )
    )
    try:
        await asyncio.to_thread(finish, value.history, value.tasks[0])
        result = await asyncio.wait_for(waiter, 10)
        assert not result["pending"] and "interrupted" not in result
    finally:
        waiter.cancel()
        await asyncio.gather(waiter, return_exceptions=True)
