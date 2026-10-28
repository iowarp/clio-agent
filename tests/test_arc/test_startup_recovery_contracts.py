"""Startup attention recovers durable partial writes without fabricating settlement."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from clio_agent.arc.memory import ARCMemory
from clio_agent.arc.task_types import MailItem
from clio_agent.gact.conversation_assignments import _finish_segment
from clio_agent.gact.conversations import ConversationOutcome, ConversationSegment
from clio_agent.gact.native_work import now
from clio_agent.gact.session_input import accept_session_input
from clio_agent.gact.sessions import SessionStore
from clio_agent.gact.task_access import TaskCaller
from clio_agent.gact.task_history import TaskHistory
from clio_agent.gact.task_operations import TaskOperations
from tests.test_arc.test_conversation_owner_contracts import create_environment
from tests.test_arc.test_task_replacement_contracts import forbid_cancel


@pytest.mark.asyncio
async def test_startup_attention_recovers_after_parent_write_before_self_input(
    arc: ARCMemory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, owner, root = await create_environment(arc, tmp_path)
    caller = TaskCaller(root.owner.session_id, "human")
    accepted = await owner.spawn(caller, "Retained child", "worker", "partial-startup-notice")
    sid = accepted["session_id"]
    session = app.state.sessions.get(sid)
    assert session is not None
    session.creation_lifecycle_confirmed = True
    session.creation_hook_state = "interrupted"
    history = owner.history
    record = history.record_mail

    def lose_reply(item: MailItem, *, max_pending_messages: int = 0) -> MailItem:
        stored = record(item, max_pending_messages=max_pending_messages)
        if item.kind == "attention" and item.recipient_session == caller.session_id:
            raise OSError("Lost reply after the parent's confirmed Core mail write")
        return stored

    monkeypatch.setattr(history, "record_mail", lose_reply)
    try:
        with pytest.raises(OSError, match="Lost reply"):
            await asyncio.to_thread(owner.note_startup_interruption, sid)
        assert history.requires_recovery
        recovered = await asyncio.to_thread(TaskHistory, arc)
        await asyncio.to_thread(recovered.restore)
        owner.history = app.state.task_history = recovered
        owner.mail.history = owner.recovery.history = recovered
        assert not [
            item
            for item in (await asyncio.to_thread(recovered.pending_mail, sid))
            if item.id.startswith("startup-interrupted:")
        ]
        await asyncio.to_thread(owner.note_startup_interruption, sid)
        await asyncio.to_thread(owner.note_startup_interruption, sid)
        parent_notices = [
            item
            for item in (await asyncio.to_thread(recovered.pending_mail, caller.session_id))
            if item.kind == "attention"
        ]
        child_notices = [
            item
            for item in (await asyncio.to_thread(recovered.pending_mail, sid))
            if item.id.startswith("startup-interrupted:")
        ]
        assert len(parent_notices) == len(child_notices) == 1
        assert child_notices[0].kind == "hook"
        assert parent_notices[0].content == child_notices[0].content
        body = await asyncio.to_thread(recovered.read_content, child_notices[0].content)
        assert isinstance(body, dict)
        assert body["effects"] == "unconfirmed" and body["cleanup"] == "confirmed"
        assert not recovered.acknowledged_notifications(sid)
        assert not recovered.acknowledged_notifications(caller.session_id)
    finally:
        await owner.shutdown()
        await owner.recovery.drain()


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy_terminal", [False, True])
@pytest.mark.parametrize("reconstruct", [False, True])
async def test_failed_startup_never_admits_a_successor_without_physical_proof(
    arc: ARCMemory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    legacy_terminal: bool,
    reconstruct: bool,
) -> None:
    """Exercise failure recording and both successor paths with retained Core data."""
    app, owner, root = await create_environment(arc, tmp_path)
    caller = TaskCaller(root.owner.session_id, "human", human_action=True)
    accepted = await owner.spawn(
        caller, "Startup effects need inspection", "worker", "failed-startup"
    )
    sid, handle = accepted["session_id"], accepted["handle"]
    effect = tmp_path / "startup-effect.txt"

    def failed_start(**_kwargs: Any) -> None:
        effect.write_text("an effect occurred before the cleanup receipt was lost")
        raise RuntimeError("startup cleanup receipt lost")

    monkeypatch.setattr("clio_agent.gact.hooks.dispatch_session_start", failed_start)
    # A configured hook (the fake above) runs, so creation records its "started" receipt.
    monkeypatch.setattr("clio_agent.gact.hooks.session_start_may_run_hooks", lambda _sid: True)
    try:
        with pytest.raises(RuntimeError, match="cleanup receipt lost"):
            await asyncio.to_thread(owner.sessions.initialize, sid)
        assignment = await asyncio.to_thread(owner.history.task_snapshot, handle)
        assert assignment is not None
        trigger = (await asyncio.to_thread(owner.history.pending_mail, sid))[0]
        await asyncio.to_thread(
            _finish_segment,
            owner,
            ConversationSegment(sid, assignment, "startup-failed-turn", trigger, "original work"),
            ConversationOutcome(
                {}, error={"type": "RuntimeError", "message": "startup cleanup receipt lost"}
            ),
        )
        failed = await asyncio.to_thread(owner.history.task_snapshot, handle)
        assert failed is not None and failed.status == "task_state_uncertain"
        assert failed.terminal_at is None and failed.settlement == "unknown"
        assert failed.details is not None
        if legacy_terminal:
            # Preserve an older falsely-confirmed terminal outcome as historical
            # data; even that row cannot authorize new execution in this session.
            failed = failed.model_copy(
                update={
                    "status": "failed",
                    "raw_status": "failed",
                    "terminal_at": now(),
                    "settlement": "confirmed",
                }
            )
            await asyncio.to_thread(
                owner.history.record_snapshot, failed, observation_id="legacy-terminal"
            )
        if reconstruct:
            owner.sessions = app.state.sessions = await asyncio.to_thread(
                SessionStore, path=tmp_path / "sessions.json"
            )
            await owner.history.io.close()
            owner.history = app.state.task_history = await asyncio.to_thread(TaskHistory, arc)
            await owner.history.io.run(owner.history.restore)
            owner.mail.history = owner.recovery.history = owner.history
            restored = owner.sessions.get(sid)
            assert restored is not None and restored.creation_hook_state == "started"
            assert (await asyncio.to_thread(owner.history.task_snapshot, handle)) == failed
        app.state.task_operations = TaskOperations(owner.history, forbid_cancel)
        before = await asyncio.to_thread(owner.history.task_handles, kind="Subagent")
        message_id = await asyncio.to_thread(
            accept_session_input, app, sid, "Inspect the retained failure", source="hook"
        )
        assert await owner._admit(sid) is None
        acknowledgement = await owner.restart(caller, handle)
        assert acknowledgement["restart_requested"]
        await asyncio.gather(*tuple(owner._controls.values()), return_exceptions=True)
        assert await owner._admit(sid) is None
        assert await asyncio.to_thread(owner.history.task_handles, kind="Subagent") == before
        latest = await asyncio.to_thread(owner.history.latest_assignment, sid)
        assert latest is not None and latest.handle == handle
        assert any(
            item.id == message_id
            for item in (await asyncio.to_thread(owner.history.pending_mail, sid))
        )
        assert effect.read_text() == "an effect occurred before the cleanup receipt was lost"
        assert not owner.runner.busy(sid)
    finally:
        await owner.shutdown()
        await owner.recovery.drain()


@pytest.mark.asyncio
async def test_unknown_startup_settlement_prevents_a_restart_successor(
    arc: ARCMemory, tmp_path: Path
) -> None:
    app, owner, root = await create_environment(arc, tmp_path)
    caller = TaskCaller(root.owner.session_id, "human", human_action=True)
    accepted = await owner.spawn(caller, "Unconfirmed startup", "worker", "unknown-startup")
    sid = accepted["session_id"]
    session = app.state.sessions.get(sid)
    assert session is not None and session.creation_digest
    # Historical loss of an attempt's cleanup receipt is uncertainty, not proof
    # of either a live process or successful cleanup. No fake workload is launched.
    session.creation_lifecycle_confirmed = True
    session.creation_hook_state = "started"
    operations = app.state.task_operations = TaskOperations(owner.history, forbid_cancel)
    try:
        acknowledgement = await owner.restart(caller, accepted["handle"])
        assert acknowledgement["restart_requested"] is True
        await asyncio.gather(*tuple(owner._controls.values()), return_exceptions=True)
        current = await asyncio.to_thread(owner.history.task_snapshot, accepted["handle"])
        assert current is not None
        assert current.status == "task_state_uncertain"
        assert current.terminal_at is None and current.settlement == "unknown"
        assert "SessionStart process settlement is unconfirmed" in current.progress.message
        assert (await asyncio.to_thread(owner.history.latest_assignment, sid)) == current
        assert len(await asyncio.to_thread(owner.history.task_handles, kind="Subagent")) == 1
        outcome = await operations.wait(caller, accepted["handle"], timeout_s=0)
        assert accepted["handle"] in outcome["pending"]
        assert not owner.runner.busy(sid)
        assert session.creation_hook_state == "started"
        assert not owner.history.acknowledged_notifications(caller.session_id)
    finally:
        await owner.shutdown()
        await owner.recovery.drain()


@pytest.mark.asyncio
@pytest.mark.parametrize("activity", ["busy", "recovering"])
async def test_restart_settles_an_interrupted_startup_so_its_parent_receives_an_outcome(
    arc: ARCMemory, tmp_path: Path, activity: str
) -> None:
    """A restart after a kill inside SessionStart: the child is settled, never stranded.

    Its admission stays closed and no hook is replayed, but the work is an obligation
    of this start: the assignment ends ``task_state_uncertain`` with the startup reason,
    and a parent waiting on it (a flow's gather) receives that typed outcome.
    """
    from clio_agent.gact.conversation_recovery import _restore_assignment

    app, owner, root = await create_environment(arc, tmp_path)
    caller = TaskCaller(root.owner.session_id, "human")
    accepted = await owner.spawn(caller, "Killed during startup", "worker", "killed-startup")
    sid, handle = accepted["session_id"], accepted["handle"]
    session = app.state.sessions.get(sid)
    assert session is not None and session.creation_digest
    session.creation_lifecycle_confirmed = True
    session.creation_hook_state = "started"  # the receipt a kill left behind
    task = await asyncio.to_thread(owner.history.task_snapshot, handle)
    assert task is not None
    interrupted = task.model_copy(update={"activity": activity, "updated_at": now()})
    await asyncio.to_thread(owner.history.record_snapshot, interrupted, observation_id="kill")
    operations = app.state.task_operations = TaskOperations(owner.history, forbid_cancel)
    try:
        await _restore_assignment(owner, handle)
        current = await asyncio.to_thread(owner.history.task_snapshot, handle)
        assert current is not None and current.status == "task_state_uncertain"
        assert current.terminal_at is None and current.settlement == "unknown"
        assert "SessionStart process settlement is unconfirmed" in current.progress.message
        assert current.attention_episode is not None
        pending = await asyncio.to_thread(owner.history.pending_mail, sid)
        assert not [item for item in pending if item.id.startswith("recovery:")]
        waited = await asyncio.wait_for(operations.wait(caller, handle), 10)
        assert {"handle": handle, "status": "task_state_uncertain"} in waited["outcomes"]
        assert await owner._admit(sid) is None  # still never admitted: no hook replay
        assert session.creation_hook_state == "started"
        await _restore_assignment(owner, handle)  # idempotent across restarts
        assert await asyncio.to_thread(owner.history.task_snapshot, handle) == current
    finally:
        await owner.shutdown()
        await owner.recovery.drain()
