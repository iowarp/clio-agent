"""Replacement public contracts on real Core; these are not model acceptance."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Callable
from typing import Any

import pytest

from clio_agent.arc.memory import ARCMemory
from clio_agent.arc.task_types import (
    AssignmentDetails,
    BackendReference,
    ContextBoundary,
    CoordinationGrant,
    CoreReference,
    NativeAttempt,
    QuestionDefinition,
    QuestionReply,
    RecipientInput,
    TaskAlias,
    TaskOwner,
    TaskProgress,
    TaskSnapshot,
    TaskSubscription,
)
from clio_agent.gact.questions import QuestionCommands
from clio_agent.gact.session_mail import SessionMail
from clio_agent.gact.task_access import TaskCaller
from clio_agent.gact.task_history import TaskHistory, _encoded
from clio_agent.gact.task_operations import TaskOperations
from clio_agent.gact.task_publication import publish_task_notice

STAMP = "2026-10-14T04:00:00Z"


def record_task(
    history: TaskHistory,
    handle: str,
    *,
    session: str = "owner",
    description: str = "Original assignment",
    human: str = "human",
) -> TaskSnapshot:
    """Publish a workload owner's observation through the production Core writer."""
    task = TaskSnapshot(
        handle=handle,
        kind="Shell",
        tool="bash",
        label="Command",
        owner=TaskOwner(human_id=human, session_id=session, assignment_id="assignment_" + session),
        invocation_id=handle,
        created_at=STAMP,
        updated_at=STAMP,
        description=description,
        description_missing=False,
        status="running",
        supported_actions=("observe", "wait", "cancel", "get_result"),
    )
    history.record_snapshot(task, observation_id="start:" + handle)
    recorded = history.task_snapshot(handle)
    assert recorded is not None
    return recorded


def finish(history: TaskHistory, task: TaskSnapshot, *, status: str = "completed") -> None:
    """Supply an independent owner's settled observation, without a fake execution engine."""
    result = history.write_content(task.owner.session_id, {"output": task.handle, "status": status})
    settled = task.model_copy(
        update={
            "status": status,
            "raw_status": status,
            "terminal_at": STAMP,
            "settlement": "confirmed",
            "result": result,
        }
    )
    history.record_snapshot(settled, observation_id="finish:" + task.handle)
    publish_task_notice(history, settled)


async def forbid_cancel(task: TaskSnapshot, caller: TaskCaller) -> dict[str, Any]:
    """A read/wait case must never reach an execution owner's cancellation path."""
    pytest.fail(f"Unexpected cancellation of {task.handle} by {caller.session_id}")


@pytest.mark.asyncio
@pytest.mark.parametrize("control", ["wait", "observe"])
async def test_legacy_alias_wakes_on_the_canonical_owner_observation(
    arc: ARCMemory,
    monkeypatch: pytest.MonkeyPatch,
    control: str,
) -> None:
    history = TaskHistory(arc)
    task = await asyncio.to_thread(record_task, history, "task_canonical")
    await asyncio.to_thread(
        history.record_alias,
        TaskAlias(alias="old-id", handle=task.handle, source_identity="legacy"),
    )
    registered = asyncio.Event()
    original = history.listen

    def listen(
        callback: Callable[[int], None],
        *,
        handles: tuple[str, ...] = (),
        recipient_session: str | None = None,
    ) -> Callable[[], None]:
        remove = original(callback, handles=handles, recipient_session=recipient_session)
        assert handles == (task.handle,)
        registered.set()
        return remove

    monkeypatch.setattr(history, "listen", listen)
    operations, caller = TaskOperations(history, forbid_cancel), TaskCaller("owner", "human")
    waiter = asyncio.create_task(
        operations.wait(caller, "old-id")
        if control == "wait"
        else operations.observe(caller, "old-id", pattern="never-matches-output")
    )
    try:
        await asyncio.wait_for(registered.wait(), 1)
        await asyncio.to_thread(finish, history, task)
        result = await asyncio.wait_for(waiter, 1)
        assert not result.get("timed_out", False)
        rows = result["outcomes"] if control == "wait" else result["tasks"]
        assert rows[0]["handle"] == task.handle and rows[0]["status"] == "completed"
    finally:
        if not waiter.done():
            waiter.cancel()
        await asyncio.gather(waiter, return_exceptions=True)


@pytest.mark.asyncio
async def test_uncertain_legacy_status_does_not_require_an_episode_to_end_wait(
    arc: ARCMemory,
) -> None:
    history = TaskHistory(arc)
    task = await asyncio.to_thread(record_task, history, "task_uncertain")
    await asyncio.to_thread(
        history.record_snapshot,
        task.model_copy(update={"status": "task_state_uncertain", "settlement": "unknown"}),
        observation_id="uncertain-without-episode",
    )
    result = await asyncio.wait_for(
        TaskOperations(history, forbid_cancel).wait(TaskCaller("owner", "human"), task.handle), 1
    )
    assert result["pending"] == [task.handle] and not result["timed_out"]
    assert result["outcomes"] == [{"handle": task.handle, "status": "task_state_uncertain"}]
    assert history.acknowledged_notifications("owner") == frozenset()


@pytest.mark.asyncio
async def test_a_cancellation_still_settling_keeps_the_wait_going(arc: ARCMemory) -> None:
    """While a member's cancellation job runs, its interim uncertain mark is progress:
    the wait goes on until the member settles. If the job ends with the member still
    uncertain, the wait wakes and reports it."""

    from types import SimpleNamespace

    history = TaskHistory(arc)
    jobs: dict[str, asyncio.Task[None]] = {}

    def running(handle: str) -> asyncio.Task[None] | None:
        job = jobs.get(handle)
        return job if job is not None and not job.done() else None

    conversations = SimpleNamespace(cancellation_job=running)
    app = SimpleNamespace(state=SimpleNamespace(conversations=conversations))
    operations = TaskOperations(history, forbid_cancel, app=app)
    caller = TaskCaller("owner", "human")

    async def uncertain(handle: str) -> TaskSnapshot:
        task = await asyncio.to_thread(record_task, history, handle)
        await asyncio.to_thread(
            history.record_snapshot,
            task.model_copy(
                update={
                    "status": "task_state_uncertain",
                    "settlement": "unknown",
                    "attention_episode": "cancelling",
                }
            ),
            observation_id=handle + "-uncertain",
        )
        return task

    release = asyncio.Event()
    settles = await uncertain("task_settles")
    jobs[settles.handle] = asyncio.create_task(release.wait())
    waiter = asyncio.create_task(operations.wait(caller, settles.handle))
    await asyncio.sleep(0.3)
    assert not waiter.done()
    await asyncio.to_thread(finish, history, settles, status="cancelled")
    result = await asyncio.wait_for(waiter, 2)
    assert result["pending"] == [] and result["outcomes"][0]["status"] == "cancelled"

    stays = await uncertain("task_stays_uncertain")
    jobs[stays.handle] = asyncio.create_task(release.wait())
    waiter = asyncio.create_task(operations.wait(caller, stays.handle))
    await asyncio.sleep(0.3)
    assert not waiter.done()
    release.set()
    result = await asyncio.wait_for(waiter, 2)
    assert result["pending"] == [stays.handle]
    assert result["outcomes"] == [{"handle": stays.handle, "status": "task_state_uncertain"}]


def test_restore_coalesces_hints_without_losing_pending_core_mail(arc: ARCMemory) -> None:
    history = TaskHistory(arc)
    tasks = [record_task(history, "task_replay_" + str(index)) for index in range(3)]
    for task in tasks:
        finish(history, task)
    restored = TaskHistory(arc)
    hints: list[int] = []
    mail: list[str] = []
    restored.listen(hints.append)
    restored.listen_mail(mail.append)
    restored.restore()
    assert hints == [restored.revision] and mail == ["owner"]
    assert {item.handle for item in restored.pending_mail("owner")} == {
        task.handle for task in tasks
    }


def test_description_reference_survives_core_restore(arc: ARCMemory) -> None:
    history = TaskHistory(arc)
    original = record_task(
        history, "task_description", description="Transfer selected analysis files"
    )
    retained = history.task_snapshot(original.handle)
    assert retained is not None and retained.description_reference is not None
    assert history.read_content(retained.description_reference) == original.description
    restored = TaskHistory(arc)
    restored.restore()
    assert restored.task_snapshot(original.handle) == retained


def test_progress_replay_reads_immutable_assignment_content_once(
    arc: ARCMemory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Retain every progress event without rereading unchanged assignment bodies per event."""
    history = TaskHistory(arc)
    details = history.write_content("owner", AssignmentDetails().model_dump(mode="json"))
    task = TaskSnapshot(
        handle="task_session_replay",
        kind="Session",
        tool="conversation",
        label="Assignment",
        owner=TaskOwner(human_id="human", session_id="owner", assignment_id="task_session_replay"),
        invocation_id="human",
        created_at=STAMP,
        updated_at=STAMP,
        description="Original immutable assignment",
        description_missing=False,
        status="active",
        details=details,
    )
    for index in range(32):
        history.record_snapshot(
            task.model_copy(update={"progress": TaskProgress(message=str(index))}),
            observation_id="progress:" + str(index),
        )
    original = history.task_snapshot(task.handle)
    assert original is not None and original.description_reference is not None
    reads: list[CoreReference] = []
    read_content = TaskHistory.read_content

    def counted(self: TaskHistory, reference: CoreReference) -> Any:
        reads.append(reference)
        return read_content(self, reference)

    monkeypatch.setattr(TaskHistory, "read_content", counted)
    restored = TaskHistory(arc)
    restored.restore()
    assert restored.task_snapshot(task.handle) == original
    assert reads.count(original.description_reference) == 1
    assert reads.count(details) == 1
    observations = [
        event
        for event in arc._live.iter_event_contents()
        if event.get("event_type") == "task.observed"
    ]
    assert len(observations) == 32


@pytest.mark.asyncio
async def test_expired_mixed_wait_preserves_running_members(arc: ARCMemory) -> None:
    history = TaskHistory(arc)
    running = await asyncio.to_thread(record_task, history, "task_running")
    failed = await asyncio.to_thread(record_task, history, "task_failed")
    await asyncio.to_thread(finish, history, failed, status="failed")
    operations = TaskOperations(history, forbid_cancel)
    caller = TaskCaller("owner", "human")
    result = await operations.wait(
        caller, [running.handle, failed.handle, "task_missing"], timeout_s=0
    )
    assert result["pending"] == [running.handle]
    outcomes = {item["handle"]: item for item in result["outcomes"]}
    assert outcomes[failed.handle]["status"] == "failed"
    assert outcomes["task_missing"]["error"] == "task_unavailable"
    current = await asyncio.to_thread(history.task_snapshot, running.handle)
    assert current is not None and current.status == "running"
    assert current.cancellation_requested_at is None


@pytest.mark.asyncio
async def test_any_wait_reports_member_error_and_leaves_valid_work_pending(arc: ARCMemory) -> None:
    """Preserve the shipped any=(settled or errors) contract without cancelling members."""
    history = TaskHistory(arc)
    running = await asyncio.to_thread(record_task, history, "task_running")
    result = await asyncio.wait_for(
        TaskOperations(history, forbid_cancel).wait(
            TaskCaller("owner", "human"), ["task_missing", running.handle], return_when="any"
        ),
        2,
    )
    assert result == {
        "outcomes": [{"handle": "task_missing", "error": "task_unavailable"}],
        "pending": [running.handle],
        "timed_out": False,
    }
    current = await asyncio.to_thread(history.task_snapshot, running.handle)
    assert (
        current is not None
        and current.status == "running"
        and current.cancellation_requested_at is None
    )


@pytest.mark.asyncio
async def test_revocation_releases_unbounded_peer_wait_without_task_progress(
    arc: ARCMemory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A grant change alone must wake the wait and recheck disclosure rights."""
    history = TaskHistory(arc)
    running = await asyncio.to_thread(record_task, history, "task_peer_wait")
    grant = CoordinationGrant(
        id="grant-peer-wait",
        granting_human="human",
        recipient_session="peer",
        target_session="owner",
        rights=("read", "observe"),
        created_at=STAMP,
    )
    await asyncio.to_thread(history.record_grant, grant, observation_id="grant-peer-wait")
    registered = asyncio.Event()
    original = history.listen

    def listen(
        callback: Callable[[int], None],
        *,
        handles: tuple[str, ...] | None = None,
        recipient_session: str | None = None,
    ) -> Callable[[], None]:
        remove = original(callback, handles=handles, recipient_session=recipient_session)
        registered.set()
        return remove

    monkeypatch.setattr(history, "listen", listen)
    operations = TaskOperations(history, forbid_cancel)
    waiter = asyncio.create_task(operations.wait(TaskCaller("peer", "human"), running.handle))
    try:
        await registered.wait()
        assert not waiter.done()
        await asyncio.to_thread(
            history.record_grant,
            grant.model_copy(update={"revoked_at": STAMP}),
            observation_id="grant-peer-revoked",
        )
        result = await waiter
        assert result == {
            "outcomes": [{"handle": running.handle, "error": "task_unavailable"}],
            "pending": [],
            "timed_out": False,
        }
        current = await asyncio.to_thread(history.task_snapshot, running.handle)
        assert current == running and current.terminal_at is None
        assert current.cancellation_requested_at is None
    finally:
        if not waiter.done():
            waiter.cancel()
        await asyncio.gather(waiter, return_exceptions=True)


@pytest.mark.asyncio
async def test_waiter_cancellation_leaves_question_open(arc: ARCMemory) -> None:
    history = TaskHistory(arc)
    questions = QuestionCommands(history)
    owner = TaskOwner(human_id="human", session_id="owner", assignment_id="assignment_owner")
    question = await asyncio.to_thread(
        questions.create,
        owner,
        QuestionDefinition(prompt="Which dataset should I analyze?"),
        invocation_id="ask-1",
    )
    operations = TaskOperations(history, forbid_cancel)
    waiter = asyncio.create_task(operations.wait(TaskCaller("owner", "human"), question.handle))
    await asyncio.sleep(0)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    current = await asyncio.to_thread(history.task_snapshot, question.handle)
    assert current is not None and current.status == "input_required"
    assert current.cancellation_requested_at is None
    await asyncio.to_thread(questions.answer, question.handle, QuestionReply(answer="Dataset B"))
    collected = await operations.wait(TaskCaller("owner", "human"), question.handle, timeout_s=0)
    assert collected["pending"] == []
    assert collected["outcomes"][0]["status"] == "completed"


def test_question_creation_recovers_unposted_definition(
    arc: ARCMemory, monkeypatch: pytest.MonkeyPatch
) -> None:
    history = TaskHistory(arc)
    questions = QuestionCommands(history)
    definition = QuestionDefinition(prompt="Approve the destination?")
    original = questions.post

    def fail_post(accepted: TaskSnapshot, definition: QuestionDefinition) -> TaskSnapshot:
        raise OSError("crash before posting")

    monkeypatch.setattr(questions, "post", fail_post)
    with pytest.raises(OSError, match="before posting"):
        questions.create(
            TaskOwner(human_id="human", session_id="owner", assignment_id="assignment_owner"),
            definition,
            invocation_id="ask-recover",
            handle="task_question_recover",
        )
    retained = history.task_snapshot("task_question_recover")
    assert retained is not None and retained.details is not None
    assert retained.raw_status == ""
    assert QuestionDefinition.model_validate(history.read_content(retained.details)) == definition
    monkeypatch.setattr(questions, "post", original)
    questions.restore()
    posted = history.task_snapshot(retained.handle)
    assert posted is not None and posted.status == "input_required"
    assert (
        questions.create(
            posted.owner, definition, invocation_id="ask-recover", handle=posted.handle
        )
        == posted
    )


def test_revoking_observe_holds_published_subscriber_input(arc: ARCMemory) -> None:
    history = TaskHistory(arc)
    task = record_task(history, "task_subscription")
    grant = CoordinationGrant(
        id="grant-observe",
        granting_human="human",
        recipient_session="peer",
        target_session="owner",
        rights=("read", "observe"),
        created_at=STAMP,
    )
    history.record_grant(grant, observation_id="grant-initial")
    history.record_subscription(
        TaskSubscription(
            handle=task.handle,
            recipient_session="peer",
            recipient_human="human",
            enabled=True,
            updated_at=STAMP,
        )
    )
    finish(history, task)
    mailbox = SessionMail(history, lambda _sid, _item: True)
    peer = TaskCaller("peer", "human")
    staged = mailbox.stage(peer)
    assert len(staged.items) == 1
    history.record_grant(
        grant.model_copy(update={"revoked_at": STAMP}), observation_id="grant-revoke"
    )
    history.record_grant(
        grant.model_copy(update={"id": "grant-read", "rights": ("read",)}),
        observation_id="grant-read-only",
    )
    assert mailbox.stage(peer).items == ()
    assert len(history.pending_mail("peer")) == 1
    assert len(history.pending_mail("owner")) == 1
    with pytest.raises(ValueError, match="authorization changed"):
        mailbox.commit(
            peer,
            staged,
            assignment_id="peer-assignment",
            iteration_id="peer-iteration",
            boundary=ContextBoundary(session_id="peer", scope="main", logical_time=0),
        )
    assert history.acknowledged_notifications("peer") == frozenset()
    assert TaskOperations(history, forbid_cancel).result(peer, task.handle)["status"] == "completed"


def test_query_filters_and_pagination_never_count_unrelated_tasks(arc: ARCMemory) -> None:
    history = TaskHistory(arc)
    first = record_task(history, "task_a")
    second = record_task(history, "task_b")
    record_task(history, "task_hidden", session="unrelated", description="Private assignment")
    operations = TaskOperations(history, forbid_cancel)
    caller = TaskCaller("owner", "human")
    page = operations.query(caller, limit=1)
    assert page["total"] == 2
    assert [row["handle"] for row in page["tasks"]] == [first.handle]
    restored = TaskHistory(arc)
    restored.restore()
    following = TaskOperations(restored, forbid_cancel).query(
        caller, cursor=page["cursor"], limit=1
    )
    assert following["total"] == 2
    assert [row["handle"] for row in following["tasks"]] == [second.handle]
    assert operations.query(caller, handle="task_hidden")["tasks"] == []


def test_collection_receipt_cannot_consume_another_recipient(arc: ARCMemory) -> None:
    history = TaskHistory(arc)
    task = record_task(history, "task_recipient")
    history.record_grant(
        CoordinationGrant(
            id="grant-peer",
            granting_human="human",
            recipient_session="peer",
            target_session="owner",
            rights=("read", "observe"),
            created_at=STAMP,
        ),
        observation_id="grant-peer",
    )
    history.record_subscription(
        TaskSubscription(
            handle=task.handle,
            recipient_session="peer",
            recipient_human="human",
            enabled=True,
            updated_at=STAMP,
        )
    )
    finish(history, task)
    (peer_item,) = history.pending_mail("peer")
    history.record_input(
        RecipientInput(
            id="peer-input",
            session_id="peer",
            assignment_id="peer-assignment",
            iteration_id="peer-iteration",
            boundary=ContextBoundary(session_id="peer", scope="main", logical_time=0),
            notification_ids=(peer_item.id,),
            content=(peer_item.content,),
            committed_at=STAMP,
        )
    )
    assert history.pending_mail("peer") == ()
    assert len(history.pending_mail("owner")) == 1
    assert (
        TaskOperations(history, forbid_cancel).result(TaskCaller("peer", "human"), task.handle)[
            "status"
        ]
        == "completed"
    )


def test_cleanup_receipt_cannot_settle_another_incarnation(arc: ARCMemory) -> None:
    history = TaskHistory(arc)
    task = record_task(history, "task_incarnation")
    backend = BackendReference(
        owner="native", server="clio-shell", session="runtime", task_id="native-1"
    )
    history.record_snapshot(
        task.model_copy(update={"backend": backend}), observation_id="bind-native"
    )
    first = NativeAttempt(
        handle=task.handle,
        incarnation="first",
        backend=backend,
        leg=1,
        started_at=STAMP,
        execution_key="execution-first",
    )
    second = first.model_copy(
        update={"incarnation": "second", "leg": 2, "execution_key": "execution-second"}
    )
    history.record_attempt(first)
    history.record_attempt(second)
    receipt = history.write_content(
        "owner",
        {
            "incarnation": first.incarnation,
            "execution_key": first.execution_key,
            "physical_cleanup": "confirmed",
        },
    )
    with pytest.raises(ValueError, match="another native incarnation"):
        history.record_attempt(
            second.model_copy(
                update={
                    "cleanup_receipt": receipt,
                    "cleanup_state": "confirmed",
                    "finished_at": STAMP,
                }
            )
        )
    assert history.native_attempts(task.handle)[1].cleanup_state == "pending"


def test_explicit_collection_of_held_result_uses_its_durable_observation(arc: ARCMemory) -> None:
    history = TaskHistory(arc)
    task = record_task(history, "task_held")
    finish(history, task)
    (notice,) = history.pending_mail("owner")
    held = notice.model_copy(
        update={"delivery": "held", "held_reason": "historical_delivery_unknown", "revision": 2}
    )
    history.update_mail(held, expected_revision=1)
    caller = TaskCaller("owner", "human")
    mailbox = SessionMail(history, lambda _sid, _item: False)
    assert mailbox.stage(caller).items == ()
    staged = mailbox.collection(caller, (held.id,))
    body = {"call_id": "collect", "text": "Already collected", "notification_ids": [held.id]}
    observation = arc.append_segment("owner", "main", "observation", body)
    mailbox.commit(
        caller,
        staged,
        assignment_id="assignment",
        iteration_id="collection",
        boundary=ContextBoundary(session_id="owner", scope="main", logical_time=0),
        tool_observation=CoreReference(
            session_id="owner",
            scope="main",
            content_id=str(observation.id),
            sha256=hashlib.sha256(_encoded(body)).hexdigest(),
        ),
    )
    assert history.pending_mail("owner") == ()
    assert held.id in history.acknowledged_notifications("owner")
    restored = TaskHistory(arc)
    restored.restore()
    assert restored.pending_mail("owner") == ()
    assert held.id in restored.acknowledged_notifications("owner")
    assert restored.committed_observations("owner", "main") == frozenset({str(observation.id)})
