"""Conversation outcomes keep their canonical result across interrupted publication."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from clio_agent.arc.memory import ARCMemory
from clio_agent.arc.task_types import (
    CoordinationGrant,
    DeferredCompletion,
    MailItem,
    TaskSubscription,
)
from clio_agent.gact.conversation_assignments import _finish_segment
from clio_agent.gact.conversations import ConversationOutcome, Conversations, ConversationSegment
from clio_agent.gact.core_recovery import CoreRecovery
from clio_agent.gact.native_work import now
from clio_agent.gact.questions import QuestionCommands
from clio_agent.gact.task_history import TaskHistory
from tests.test_arc.test_conversation_owner_contracts import create_environment


@pytest.mark.asyncio
@pytest.mark.parametrize("child", [False, True])
@pytest.mark.parametrize("deferred", [False, True])
@pytest.mark.parametrize("restart", [False, True])
@pytest.mark.parametrize("failed", [False, True])
async def test_terminal_assignment_recovers_owner_and_subscriber_notices(
    arc: ARCMemory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    child: bool,
    deferred: bool,
    restart: bool,
    failed: bool,
) -> None:
    """Every finish route repairs its saved outcome, without another model call."""
    app, owner, root = await create_environment(arc, tmp_path)
    history = owner.history
    task = root
    if child:
        receipt = await owner.spawn(
            owner.caller(root.owner.session_id),
            "Retained child assignment",
            "worker",
            "child-with-result",
            start=False,
        )
        task = history.task_snapshot(receipt["handle"])
        assert task is not None

    def prepare() -> tuple[ConversationSegment, DeferredCompletion]:
        history.record_grant(
            CoordinationGrant(
                id="read-result",
                granting_human="human",
                recipient_session="peer",
                target_session=task.owner.session_id,
                target_handle=task.handle,
                rights=("read", "observe"),
                created_at=now(),
            ),
            observation_id="read-result",
        )
        history.record_subscription(
            TaskSubscription(
                handle=task.handle,
                recipient_session="peer",
                recipient_human="human",
                enabled=True,
                updated_at=now(),
            )
        )
        content = history.write_content(task.owner.session_id, {"answer": "Original result"})
        trigger = MailItem(
            id="original-input",
            recipient_session=task.owner.session_id,
            kind="human",
            sender_human="human",
            content=content,
            created_at=now(),
        )
        return ConversationSegment(
            task.owner.session_id, task, "original-turn", trigger, "Work"
        ), DeferredCompletion(result=content, failed=failed)

    segment, completion = await history.io.run(prepare)
    expected = {
        "peer": "outcome:" + task.handle + ":subscriber:peer:1",
        **(
            {root.owner.session_id: "outcome:" + task.handle + ":" + root.owner.session_id}
            if child
            else {}
        ),
    }
    fail_notice = expected[root.owner.session_id] if child else expected["peer"]
    record = history._record

    def fail_mail(
        event: str,
        sid: str,
        identity: str,
        payload: dict[str, Any],
        *,
        lane: dict[str, Any] | None = None,
    ) -> None:
        if event == "session.mail.accepted" and identity == fail_notice:
            raise OSError("Core failed after assignment terminal persistence")
        record(event, sid, identity, payload, lane=lane)

    async def finish() -> None:
        if deferred:
            await owner.complete_deferred(
                task.owner.session_id, task.handle, context_epoch="", completion=completion
            )
        else:
            await history.io.run(
                _finish_segment,
                owner,
                segment,
                ConversationOutcome(
                    {"answer": "Original result"},
                    disposition="complete",
                    error={"error": "provider_error"} if failed else None,
                ),
            )

    restored_owner = None
    restored_history = None
    monkeypatch.setattr(history, "_record", fail_mail)
    try:
        with pytest.raises(OSError, match="after assignment terminal"):
            await finish()
        saved = history.task_snapshot(task.handle)
        assert saved is not None and saved.terminal_at is not None and saved.result is not None
        assert saved.status == ("failed" if failed else "completed")
        original_result = await history.io.run(history.read_content, saved.result)
        assert history.requires_recovery
        monkeypatch.setattr(history, "_record", record)
        if restart:
            restored_history = TaskHistory(arc)
            await history.io.run(restored_history.restore)
            app.state.task_history = restored_history
            app.state.questions = QuestionCommands(restored_history)
            restored_owner = Conversations(
                app,
                restored_history,
                CoreRecovery(restored_history),
                owner.run_segment,
                owner.cancel_work,
                owner.prepare_child,
                owner.execution_gate,
            )
            app.state.conversations = restored_owner
            await restored_owner.restore()
            await restored_owner.restore()
            observed = restored_history
        else:
            await history.io.run(history.restore)
            await finish()
            await finish()
            observed = history
        assert observed.task_snapshot(task.handle) == saved
        assert await history.io.run(observed.read_content, saved.result) == original_result
        for sid, identity in expected.items():
            notices = await history.io.run(observed.pending_mail, sid)
            assert [item.id for item in notices if item.handle == task.handle] == [identity]
            assert identity not in observed.acknowledged_notifications(sid)
        assert owner.runner.handle(task.owner.session_id) is None
    finally:
        monkeypatch.setattr(history, "_record", record)
        if restored_owner is not None:
            await restored_owner.shutdown()
            await restored_owner.recovery.drain()
        if restored_history is not None:
            await restored_history.io.close()
        await owner.shutdown()
        await owner.recovery.drain()
        await history.io.close()
