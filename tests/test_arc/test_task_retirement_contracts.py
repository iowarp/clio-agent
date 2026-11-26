"""Stop, buffered input and cancellation through the real Core/conversation owners."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from clio_agent.arc.memory import ARCMemory
from clio_agent.arc.task_types import AssignmentDetails, ContextBoundary, MailItem, TaskSnapshot
from clio_agent.gact import conversation_assignments
from clio_agent.gact.conversations import ConversationOutcome, Conversations, ConversationSegment
from clio_agent.gact.native_work import now
from clio_agent.gact.questions import QuestionCommands
from clio_agent.gact.session_input import capture_retry_input
from clio_agent.gact.task_access import TaskCaller
from clio_agent.gact.task_history import TaskHistory
from clio_agent.gact.task_operations import TaskOperations
from tests.test_arc.test_conversation_owner_contracts import create_environment


@pytest.mark.asyncio
async def test_settlement_confirms_delayed_cancel_intent_before_terminal_publication(
    arc: ARCMemory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fast retirement cannot outrun the same owner's deferred intent recording."""
    _app, owner, root = await create_environment(arc, tmp_path)
    delayed: list[Callable[[], object]] = []
    observations: list[TaskSnapshot] = []
    original = owner.recovery.defer_recording

    def postpone(identity: str, operation: Callable[[], object]) -> None:
        if identity == "conversation-cancel:" + root.handle:
            delayed.append(operation)
        else:
            original(identity, operation)

    def changed(_revision: int) -> None:
        current = owner.history.task_snapshot(root.handle)
        if current is not None:
            observations.append(current)

    monkeypatch.setattr(owner.recovery, "defer_recording", postpone)
    remove = owner.history.listen(changed, handles=(root.handle,))
    caller = TaskCaller(root.owner.session_id, "human", human_action=True)
    operations = TaskOperations(owner.history, lambda task, actor: owner.cancel(actor, task.handle))
    try:
        assert (await owner.cancel(caller, root.handle))["cancellation_requested"]
        assert len(delayed) == 1
        outcome = await asyncio.wait_for(operations.wait(caller, root.handle), 10)
        assert not outcome["unfinished"] and outcome["outcomes"][0]["status"] == "cancelled"
        terminal = owner.history.task_snapshot(root.handle)
        assert terminal is not None and terminal.cancellation_requested_at is not None
        terminal_index = next(i for i, task in enumerate(observations) if task.terminal_at)
        assert any(
            task.cancellation_requested_at is not None and task.terminal_at is None
            for task in observations[:terminal_index]
        )
        assert terminal.details is not None
        details = AssignmentDetails.model_validate(
            await asyncio.to_thread(owner.history.read_content, terminal.details)
        )
        assert details.cancellation_members is not None
        await asyncio.to_thread(delayed[0])
        assert owner.history.task_snapshot(root.handle) == terminal
        restored = TaskHistory(arc)
        await asyncio.to_thread(restored.restore)
        assert restored.task_snapshot(root.handle) == terminal
    finally:
        remove()
        await owner.shutdown()
        await owner.recovery.drain()


async def accept(owner: Conversations, sid: str, identity: str, text: str) -> MailItem:
    """Confirm a human input through the existing production mailbox writer."""

    def record() -> MailItem:
        return owner.history.record_mail(
            MailItem(
                id=identity,
                recipient_session=sid,
                kind="human",
                sender_human="human",
                created_at=now(),
                content=owner.history.write_content(sid, {"text": text}),
            )
        )

    return await asyncio.to_thread(record)


async def idle(owner: Conversations, sid: str) -> None:
    """Await the runner's actual reservation release, not a display projection."""
    released = asyncio.Event()
    owner.runner.run_when_released(sid, released.set)
    await asyncio.wait_for(released.wait(), 10)


@pytest.mark.asyncio
async def test_malformed_durable_input_is_visibly_held_without_stranding_later_feedback(
    arc: ARCMemory, tmp_path: Path
) -> None:
    app, owner, root = await create_environment(arc, tmp_path)
    sid, processed = root.owner.session_id, asyncio.Event()
    observed: list[tuple[str, ...]] = []

    async def model(segment: ConversationSegment) -> ConversationOutcome:
        def commit() -> tuple[str, ...]:
            caller = owner.caller(sid)
            staged = owner.mail.stage(caller)
            owner.mail.commit(
                caller,
                staged,
                assignment_id=segment.assignment.handle,
                iteration_id=segment.turn_id,
                boundary=ContextBoundary(session_id=sid, scope="malformed-input", logical_time=0),
            )
            return tuple(item.id for item in staged.items)

        observed.append(await asyncio.to_thread(commit))
        processed.set()
        return ConversationOutcome({"answer": "Processed valid feedback"})

    owner.run_segment = model
    owner.start()

    def record_malformed() -> None:
        # Normal API submission rejects this. The recovery contract still needs
        # a visible hold for malformed retained records, rather than a retry spin.
        owner.history.record_mail(
            MailItem(
                id="malformed",
                recipient_session=sid,
                kind="human",
                sender_human="human",
                created_at=now(),
                content=owner.history.write_content(sid, {"text": 42}),
            )
        )

    try:
        await asyncio.to_thread(record_malformed)
        owner.runner.signal(sid)
        await idle(owner, sid)
        held = owner.history.mail_item(sid, "malformed")
        assert (
            held is not None
            and held.delivery == "held"
            and held.held_reason == "invalid_queued_input"
        )
        assert not observed
        owner.runner.signal(sid)
        await idle(owner, sid)
        assert owner.history.mail_item(sid, "malformed") == held and not observed
        await accept(owner, sid, "valid-feedback", "Continue independent work")
        await asyncio.wait_for(processed.wait(), 10)
        await idle(owner, sid)
        assert observed == [("valid-feedback",)]
        assert owner.history.acknowledged_notifications(sid) == frozenset({"valid-feedback"})
        assert owner.history.mail_item(sid, "malformed") == held
    finally:
        await owner.shutdown()
        await owner.recovery.drain()


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["admission", "busy_write", "final_write", "recording_failure"])
async def test_stop_retirement_preserves_buffered_feedback_without_replaying_committed_input(
    arc: ARCMemory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
) -> None:
    app, owner, root = await create_environment(arc, tmp_path)
    app.state.questions = QuestionCommands(owner.history)
    owner.start()
    # This case exercises the real pending-recovery worker. It does not need the
    # application's unrelated Question/display restoration callback.
    owner.recovery.on_ready = None
    sid, loop = root.owner.session_id, asyncio.get_running_loop()
    entered, checked, processed = asyncio.Event(), asyncio.Event(), asyncio.Event()
    release, async_release = threading.Event(), asyncio.Event()
    allow_recording = threading.Event()
    observed: list[tuple[str, ...]] = []
    failed_stop_recording = False
    real_admit, real_snapshot = owner.runner.admit, owner.history.record_snapshot
    real_finish, real_update = conversation_assignments._finish_segment, owner.history.update_mail
    first_admission = True

    async def admit(session: str) -> Any:
        nonlocal first_admission
        result = await real_admit(session)
        if phase == "admission" and first_admission and result is not None:
            first_admission = False
            entered.set()
            await async_release.wait()
        if result is None:
            checked.set()
        return result

    def snapshot(task: TaskSnapshot, **kwargs: Any) -> None:
        if (
            phase in {"busy_write", "recording_failure"}
            and task.activity == "busy"
            and not release.is_set()
        ):
            loop.call_soon_threadsafe(entered.set)
            assert release.wait(10), "Busy writer was not released"
        return real_snapshot(task, **kwargs)

    def finish(
        current_owner: Conversations, segment: ConversationSegment, outcome: ConversationOutcome
    ) -> None:
        if phase == "final_write" and not release.is_set():
            loop.call_soon_threadsafe(entered.set)
            assert release.wait(10), "Final writer was not released"
        real_finish(current_owner, segment, outcome)

    def update(item: MailItem, **kwargs: Any) -> MailItem:
        nonlocal failed_stop_recording
        if (
            phase == "recording_failure"
            and item.held_reason == "stopped_before_input_commit"
            and not allow_recording.is_set()
        ):
            failed_stop_recording = True
            raise OSError("Injected Stop-recording outage")
        return real_update(item, **kwargs)

    async def model(segment: ConversationSegment) -> ConversationOutcome:
        def commit() -> tuple[str, ...]:
            caller = owner.caller(sid)
            staged = owner.mail.stage(caller)
            owner.mail.commit(
                caller,
                staged,
                assignment_id=segment.assignment.handle,
                iteration_id=segment.turn_id,
                turn_id=segment.turn_id,
                boundary=ContextBoundary(session_id=sid, scope="r32-stop", logical_time=0),
            )
            return tuple(item.id for item in staged.items)

        assert segment.assignment.handle == root.handle
        identities = await asyncio.to_thread(commit)
        observed.append(identities)
        if "feedback" in identities:
            processed.set()
        return ConversationOutcome({"answer": "Processed " + ",".join(identities)})

    owner.runner.admit, owner.run_segment = admit, model
    monkeypatch.setattr(owner.history, "record_snapshot", snapshot)
    monkeypatch.setattr(owner.history, "update_mail", update)
    monkeypatch.setattr(conversation_assignments, "_finish_segment", finish)
    stop: asyncio.Task[None] | None = None
    try:
        await accept(owner, sid, "original", "Original work")
        await asyncio.wait_for(entered.wait(), 10)
        if phase == "final_write":
            await accept(owner, sid, "feedback", "Accepted before Stop")
        stop = asyncio.create_task(owner.runner.stop(sid))
        await asyncio.sleep(0)
        assert owner.runner.busy(sid)
        assert not stop.done()
        release.set()
        async_release.set()
        await asyncio.wait_for(stop, 10)
        if phase != "final_write":
            checked.clear()
            owner.runner.signal(sid)
            await asyncio.wait_for(checked.wait(), 10)
            assert not observed  # a hint cannot release the original trigger
            original = owner.history.mail_item(sid, "original")
            assert original is not None and original.admission is not None
            assert original.admission.assignment_id == root.handle
            if phase == "recording_failure":
                assert failed_stop_recording and owner._model_recording.get(sid)
            else:
                assert original.delivery == "held"
                assert original.held_reason == "stopped_before_input_commit"
            await accept(owner, sid, "feedback", "Continue accepted work")
            allow_recording.set()
        await asyncio.wait_for(processed.wait(), 10)
        await idle(owner, sid)
        expected: list[tuple[str, ...]] = (
            [("original",), ("feedback",)] if phase == "final_write" else [("original", "feedback")]
        )
        assert observed == expected
        assert owner.history.acknowledged_notifications(sid) == frozenset({"original", "feedback"})
        assert not await asyncio.to_thread(owner.history.pending_mail, sid)
        assert not owner._model_recording.get(sid)
        latest = owner.history.latest_assignment(sid)
        assert latest is not None and latest.handle == root.handle
    finally:
        release.set()
        allow_recording.set()
        async_release.set()
        if stop is not None:
            await asyncio.wait_for(stop, 10)
        await owner.shutdown()
        await owner.recovery.drain()


@pytest.mark.asyncio
@pytest.mark.parametrize("timing", ["during_unwind", "after_retirement", "legacy_held_input"])
async def test_trusted_retry_supersedes_stopped_input_before_ordinary_continuation(
    arc: ARCMemory,
    tmp_path: Path,
    timing: str,
) -> None:
    _, owner, root = await create_environment(arc, tmp_path)
    owner.start()
    sid = root.owner.session_id
    entered, release, processed = asyncio.Event(), asyncio.Event(), asyncio.Event()
    real_admit, held = owner.runner.admit, timing == "legacy_held_input"
    observed: list[tuple[str, ...]] = []

    async def admit(session: str) -> Any:
        nonlocal held
        result = await real_admit(session)
        if result is not None and not held:
            held = True
            entered.set()
            await release.wait()
        return result

    async def model(segment: ConversationSegment) -> ConversationOutcome:
        def commit() -> tuple[str, ...]:
            caller = owner.caller(sid)
            staged = owner.mail.stage(caller)
            owner.mail.commit(
                caller,
                staged,
                assignment_id=segment.assignment.handle,
                iteration_id=segment.turn_id,
                boundary=ContextBoundary(session_id=sid, scope="retry", logical_time=0),
            )
            return tuple(item.id for item in staged.items)

        observed.append(await asyncio.to_thread(commit))
        processed.set()
        return ConversationOutcome({"answer": "Retry processed"})

    async def retry() -> None:
        def record() -> None:
            binding = capture_retry_input(
                owner.history, sid, "original", stopping=owner.runner.stop_requested(sid)
            )
            assert binding is not None
            owner.history.record_mail(
                MailItem(
                    id="retry",
                    recipient_session=sid,
                    kind="human",
                    sender_human="human",
                    created_at=now(),
                    content=owner.history.write_content(
                        sid, {"text": "Deliberate retry", "supersedes_input": binding}
                    ),
                )
            )

        await asyncio.to_thread(record)

    owner.runner.admit, owner.run_segment = admit, model
    stop: asyncio.Task[None] | None = None
    try:
        if timing == "legacy_held_input":

            def legacy() -> None:
                owner.history.record_mail(
                    MailItem(
                        id="original",
                        recipient_session=sid,
                        kind="human",
                        sender_human="human",
                        created_at=now(),
                        delivery="held",
                        held_reason="stopped_before_input_commit",
                        content=owner.history.write_content(sid, {"text": "Retained legacy work"}),
                    )
                )

            await asyncio.to_thread(legacy)
            await retry()
        else:
            await accept(owner, sid, "original", "Original work")
            await asyncio.wait_for(entered.wait(), 10)
            reservation = owner.runner.request_stop(sid)
            assert reservation is not None
            stop = asyncio.create_task(owner.runner.stop(sid, reservation=reservation))
            if timing == "during_unwind":
                await retry()
            release.set()
            await asyncio.wait_for(stop, 10)
            if timing == "after_retirement":
                await retry()
        await asyncio.wait_for(processed.wait(), 10)
        await idle(owner, sid)
        assert observed == [("retry",)]
        assert owner.history.mail_withdrawn(sid, "original")
        assert owner.history.acknowledged_notifications(sid) == frozenset({"retry"})
        latest = owner.history.latest_assignment(sid)
        assert latest is not None and latest.handle == root.handle
    finally:
        release.set()
        if stop is not None:
            await asyncio.wait_for(stop, 10)
        await owner.shutdown()
        await owner.recovery.drain()


@pytest.mark.asyncio
@pytest.mark.parametrize("revised", [False, True])
async def test_cancel_after_stop_uses_persisted_exact_admission_and_preserves_other_inputs(
    arc: ARCMemory,
    tmp_path: Path,
    revised: bool,
) -> None:
    app, owner, root = await create_environment(arc, tmp_path)
    owner.start()
    sid = root.owner.session_id
    entered, release = asyncio.Event(), asyncio.Event()
    real_admit = owner.runner.admit

    async def admit(session: str) -> Any:
        result = await real_admit(session)
        if result is not None:
            entered.set()
            await release.wait()
        return result

    owner.runner.admit = admit
    try:
        await accept(owner, sid, "original", "First assignment input")
        await asyncio.wait_for(entered.wait(), 10)
        stopped = asyncio.create_task(owner.runner.stop(sid))
        await asyncio.sleep(0)
        release.set()
        await asyncio.wait_for(stopped, 10)
        owner.execution_gate = lambda _sid: "unrelated-human-review"
        await asyncio.to_thread(owner.history.restore)
        original = owner.history.mail_item(sid, "original")
        assert (
            original is not None and original.admission is not None and original.delivery == "held"
        )
        if revised:

            def edit() -> None:
                owner.history.update_mail(
                    original.model_copy(
                        update={
                            "content": owner.history.write_content(sid, {"text": "Human revision"}),
                            "revision": original.revision + 1,
                        }
                    ),
                    expected_revision=original.revision,
                )

            await asyncio.to_thread(edit)
        await accept(owner, sid, "other", "Unrelated accepted feedback")
        caller = TaskCaller(sid, "human", human_action=True)
        operations = TaskOperations(
            owner.history, lambda task, actor: owner.cancel(actor, task.handle)
        )
        assert (await owner.cancel(caller, root.handle))["cancellation_requested"]
        settled = await operations.wait(caller, root.handle)
        assert not settled["unfinished"] and settled["outcomes"][0]["status"] == "cancelled"
        assert owner.history.mail_withdrawn(sid, "original") is not revised
        assert not owner.history.mail_withdrawn(sid, "other")
        assert not owner.history.acknowledged_notifications(sid)
        retained = {item.id for item in await asyncio.to_thread(owner.history.pending_mail, sid)}
        assert retained == ({"original", "other"} if revised else {"other"})
    finally:
        release.set()
        await owner.shutdown()
        await owner.recovery.drain()
