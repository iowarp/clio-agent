"""Core persistence, mailbox backpressure and recoverable input boundaries."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest

from clio_agent.arc.memory import ARCMemory
from clio_agent.arc.task_types import (
    ContextBoundary,
    CoordinationGrant,
    MailItem,
    MailKind,
    QuestionDefinition,
    QuestionOption,
    QuestionReply,
    RecipientInput,
    TaskOwner,
    TaskSnapshot,
)
from clio_agent.gact.questions import QuestionCommands
from clio_agent.gact.task_history import MailboxFull, RecordingUnconfirmed, TaskHistory

STAMP = "2026-10-13T20:00:00Z"


def snapshot() -> TaskSnapshot:
    """Create an owner observation; no execution backend is fabricated."""
    return TaskSnapshot(
        handle="task_test",
        kind="Shell",
        owner=TaskOwner(human_id="human", session_id="owner", assignment_id="assignment"),
        label="Owned command",
        invocation_id="invocation",
        tool="shell_bash",
        created_at=STAMP,
        updated_at=STAMP,
    )


def mail(
    history: TaskHistory, identity: str, *, recipient: str = "owner", kind: MailKind = "peer"
) -> MailItem:
    """Confirm content before creating a mailbox reference."""
    return MailItem(
        id=identity,
        recipient_session=recipient,
        sender_session="sender",
        kind=kind,
        content=history.write_content(recipient, {"text": identity}),
        created_at=STAMP,
    )


def input_record(item: MailItem, *, identity: str = "input") -> RecipientInput:
    """Pin one model input to an existing context boundary and exact mail content."""
    return RecipientInput(
        id=identity,
        session_id=item.recipient_session,
        assignment_id="assignment",
        iteration_id="iteration",
        boundary=ContextBoundary(session_id=item.recipient_session, scope="main", logical_time=0),
        notification_ids=(item.id,),
        content=(item.content,),
        committed_at=STAMP,
    )


def test_failed_projection_does_not_publish_an_in_memory_success(
    arc: ARCMemory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A canonical observation survives failed derivation without a false local success."""
    from clio_agent.gact import task_history_projection

    history = TaskHistory(arc)
    original = task_history_projection._project

    def fail_projection(selected: TaskHistory, *args: Any, **kwargs: Any) -> None:
        if getattr(selected, "history", selected) is history:
            raise RuntimeError("injected projection failure")
        original(selected, *args, **kwargs)

    monkeypatch.setattr(task_history_projection, "_project", fail_projection)
    with pytest.raises(RuntimeError, match="projection failure"):
        history.record_snapshot(snapshot(), observation_id="observation")
    assert history.task_snapshot("task_test") is None
    with pytest.raises(RecordingUnconfirmed):
        history.record_mail(mail(history, "cannot-accept"))
    recovered = TaskHistory(arc)
    recovered.restore()
    assert recovered.task_snapshot("task_test") == snapshot()


def test_canonical_mail_and_inputs_rebuild_without_duplicate_index_io(
    arc: ARCMemory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Core history retains complete content and acknowledgements without duplicate row I/O."""
    get, put = arc._store.get, arc._store.put

    def read(kind: str, *args: Any, **kwargs: Any) -> Any:
        assert kind != "task_index", "A hot/replay read touched the retired duplicate index"
        return get(kind, *args, **kwargs)

    def write(kind: str, *args: Any, **kwargs: Any) -> Any:
        assert kind != "task_index", "A writer recreated a duplicate task/mail record"
        return put(kind, *args, **kwargs)

    monkeypatch.setattr(arc._store, "get", read)
    monkeypatch.setattr(arc._store, "put", write)
    history = TaskHistory(arc)
    for index in range(12):
        item = history.record_mail(mail(history, str(index)))
        if index < 11:
            record = input_record(item, identity="input:" + str(index)).model_copy(
                update={"iteration_id": str(index)}
            )
            history.record_input(record)
    restored = TaskHistory(arc)
    restored.restore()
    assert [item.id for item in restored.pending_mail("owner")] == ["11"]
    assert restored.acknowledged_notifications("owner") == {str(index) for index in range(11)}
    first = restored.mail_item("owner", "0")
    assert first is not None and restored.read_content(first.content) == {"text": "0"}


def test_staging_before_context_commit_remains_pending(
    arc: ARCMemory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A durable locator without its recipient-input record acknowledges nothing."""
    history = TaskHistory(arc)
    item = history.record_mail(mail(history, "before"))
    append = arc.append_segment

    def fail_input(
        session_id: str, scope: str, kind: str, content: dict[str, Any], **kwargs: Any
    ) -> Any:
        if kind == "recipient_input":
            raise OSError("crash before input commit")
        return append(session_id, scope, kind, content, **kwargs)

    monkeypatch.setattr(arc, "append_segment", fail_input)
    with pytest.raises(OSError, match="before input commit"):
        history.record_input(input_record(item))
    recovered = TaskHistory(arc)
    recovered.restore()
    assert recovered.pending_mail("owner") == (item,)


def test_crash_after_context_commit_recovers_acknowledgement(
    arc: ARCMemory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A crash before model execution cannot lose a confirmed recipient input."""
    history = TaskHistory(arc)
    item = history.record_mail(mail(history, "after"))
    record = input_record(item)

    from clio_agent.gact import task_history_projection

    accept_input = task_history_projection._accept_input

    def crash_after_commit(selected: TaskHistory, committed: RecipientInput) -> None:
        if getattr(selected, "history", selected) is history:
            raise OSError("crash after input commit")
        accept_input(selected, committed)

    monkeypatch.setattr(task_history_projection, "_accept_input", crash_after_commit)
    with pytest.raises(OSError, match="after input commit"):
        history.record_input(record)
    assert history.pending_mail("owner") == (item,)
    recovered = TaskHistory(arc)
    recovered.restore()
    assert recovered.pending_mail("owner") == ()
    assert recovered.record_input(record) == record
    assert recovered.read_content(item.content) == {"text": "after"}


def test_pending_only_peer_limit_exempts_human_and_results(arc: ARCMemory) -> None:
    """Staging/read does not release capacity; committed delivery does."""
    history = TaskHistory(arc)
    first = history.record_mail(mail(history, "peer-1"), max_pending_messages=1)
    assert history.pending_mail("owner") == (first,)
    with pytest.raises(MailboxFull):
        history.record_mail(mail(history, "peer-2"), max_pending_messages=1)
    human = history.record_mail(mail(history, "human", kind="human"), max_pending_messages=1)
    result = history.record_mail(
        mail(history, "result", kind="task_result"), max_pending_messages=1
    )
    history.record_input(input_record(first))
    second = history.record_mail(mail(history, "peer-2"), max_pending_messages=1)
    assert history.pending_mail("owner") == (human, result, second)


def test_peer_collection_does_not_consume_owner_delivery(arc: ARCMemory) -> None:
    """Recipient-specific input records leave the owner's independent notice pending."""
    history = TaskHistory(arc)
    owner = history.record_mail(mail(history, "final", kind="task_result"))
    peer = history.record_mail(mail(history, "final", recipient="peer", kind="task_result"))
    history.record_input(input_record(peer))
    assert history.pending_mail("owner") == (owner,)
    assert history.pending_mail("peer") == ()


def test_withdrawal_cannot_rewrite_committed_input(arc: ARCMemory) -> None:
    """Undelivered deletion is durable; committed input refuses silent withdrawal."""
    history = TaskHistory(arc)
    first = history.record_mail(mail(history, "withdraw"))
    history.withdraw_mail("owner", first.id)
    assert history.pending_mail("owner") == ()
    second = history.record_mail(mail(history, "committed"))
    history.record_input(input_record(second))
    with pytest.raises(ValueError, match="already committed"):
        history.withdraw_mail("owner", second.id)
    recovered = TaskHistory(arc)
    recovered.restore()
    assert recovered.pending_mail("owner") == ()


def test_legacy_missing_description_is_explicit() -> None:
    """An older row can lack assignment text without receiving an invented description."""
    row = snapshot()
    assert row.description is None
    assert row.description_missing is True
    with pytest.raises(ValueError, match="marker disagree"):
        TaskSnapshot.model_validate({**row.model_dump(), "description": "invented"})


@pytest.mark.timeout(120)  # 1001 real-Core mail commits prove history is not a lifetime cap.
def test_unlimited_mail_survives_more_than_one_thousand_messages(arc: ARCMemory) -> None:
    """History is retained while the pending index becomes empty after input commit."""
    history = TaskHistory(arc)
    reference = history.write_content("owner", {"text": "retained"})
    for index in range(1001):
        history.record_mail(
            MailItem(
                id=f"notice-{index}",
                recipient_session="owner",
                sender_session="sender",
                kind="peer",
                content=reference,
                created_at=STAMP,
            )
        )
    pending = history.pending_mail("owner")
    assert len(pending) == 1001
    history.record_input(
        RecipientInput(
            id="batch",
            session_id="owner",
            assignment_id="assignment",
            iteration_id="iteration",
            boundary=ContextBoundary(session_id="owner", scope="main", logical_time=0),
            notification_ids=tuple(item.id for item in pending),
            content=tuple(item.content for item in pending),
            committed_at=STAMP,
        )
    )
    assert history.pending_mail("owner") == ()
    recovered = TaskHistory(arc)
    recovered.restore()
    assert recovered.pending_mail("owner") == ()
    assert recovered.read_content(reference) == {"text": "retained"}
    again = history.record_mail(
        MailItem(
            id="notice-1001",
            recipient_session="owner",
            kind="peer",
            content=reference,
            created_at=STAMP,
        ),
        max_pending_messages=1,
    )
    assert history.pending_mail("owner") == (again,)


def test_question_answer_and_cancel_share_one_core_record(arc: ARCMemory) -> None:
    """Question settlement arbitrates on one record without a mirror or blocking ask."""
    history = TaskHistory(arc)
    accepted = snapshot().model_copy(update={"kind": "Question", "tool": "ask_user"})
    history.record_snapshot(accepted, observation_id="question-accepted")
    commands = QuestionCommands(history)
    posted = commands.post(accepted, QuestionDefinition(prompt="Choose a name"))
    assert posted.handle == accepted.handle
    assert posted.status == "input_required"

    def answer() -> TaskSnapshot | ValueError:
        try:
            return commands.answer(accepted.handle, QuestionReply(answer="Alice"))
        except ValueError as error:
            return error

    with ThreadPoolExecutor(max_workers=2) as pool:
        answer_future = pool.submit(answer)
        cancel_future = pool.submit(commands.cancel, accepted.handle)
        answer_outcome = answer_future.result()
        cancel_outcome = cancel_future.result()
    settled = history.task_snapshot(accepted.handle)
    assert settled is not None
    assert settled.raw_status in {"answered", "cancelled"}
    if settled.raw_status == "answered":
        assert isinstance(answer_outcome, TaskSnapshot)
        assert cancel_outcome == settled
        assert settled.result is not None
        assert QuestionReply.model_validate(history.read_content(settled.result)).answer == "Alice"
    else:
        assert isinstance(answer_outcome, ValueError)
        assert settled.result is None
    assert commands.cancel(accepted.handle) == settled


def test_content_bodies_do_not_enter_the_semantic_event_fold(arc: ARCMemory) -> None:
    """Large output and private answers remain referenced content rather than event rows."""
    history = TaskHistory(arc)
    item = history.record_mail(mail(history, "private-body"))
    events = list(arc._live.iter_event_contents())
    assert len(events) == 1
    assert events[0]["event_type"] == "session.mail.accepted"
    assert history.read_content(item.content) == {"text": "private-body"}
    assert all(
        segment.kind == "task_content" for segment in arc.list_segments("owner", item.content.scope)
    )


def test_staged_mail_cannot_commit_after_successful_withdrawal(arc: ARCMemory) -> None:
    """Human deletion wins against a batch staged before its commit boundary."""
    history = TaskHistory(arc)
    item = history.record_mail(mail(history, "withdraw-before-commit"))
    staged = input_record(item)
    history.withdraw_mail("owner", item.id)
    with pytest.raises(ValueError, match="withdrawn"):
        history.record_input(staged)
    assert not history.requires_recovery
    assert not any(
        segment.kind == "recipient_input" for segment in arc.list_segments("owner", "main")
    )


def test_collection_racing_automatic_input_cannot_deliver_twice(arc: ARCMemory) -> None:
    """Distinct staged inputs cannot acknowledge the same recipient notification twice."""
    history = TaskHistory(arc)
    item = history.record_mail(mail(history, "racing-result", kind="task_result"))
    automatic = input_record(item)
    collected = input_record(item, identity="collection").model_copy(
        update={"iteration_id": "later"}
    )
    history.record_input(collected)
    with pytest.raises(ValueError, match="already committed"):
        history.record_input(automatic)
    assert history.record_input(collected) == collected
    assert history.pending_mail("owner") == ()


def test_input_ids_are_session_scoped_and_iterations_have_one_input(arc: ARCMemory) -> None:
    """A peer's identical input ID is independent, but a model iteration is immutable."""
    history = TaskHistory(arc)
    owner = history.record_mail(mail(history, "same", recipient="owner"))
    peer = history.record_mail(mail(history, "same", recipient="peer"))
    history.record_input(input_record(owner))
    history.record_input(input_record(peer))
    extra = history.record_mail(mail(history, "next", recipient="owner"))
    with pytest.raises(ValueError, match="iteration already"):
        history.record_input(input_record(extra, identity="other-input"))
    assert history.pending_mail("owner") == (extra,)


def test_all_passive_indexes_can_be_rebuilt_without_losing_input_commit(arc: ARCMemory) -> None:
    """Canonical preparation plus context commitment ignore historical duplicate rows."""
    history = TaskHistory(arc)
    item = history.record_mail(mail(history, "durable-result", kind="task_result"))
    committed = input_record(item)
    history.record_input(committed)
    arc._store.put("task_index", "input/owner/input", b'{"stale":true}')
    recovered = TaskHistory(arc)
    recovered.restore()
    assert recovered.pending_mail("owner") == ()
    assert recovered.record_input(committed) == committed
    assert arc._store.get("task_index", "input/owner/input") == b'{"stale":true}'
    recovered.restore()
    assert recovered.pending_mail("owner") == ()


def test_post_commit_highway_failure_holds_admission_until_recovery(
    arc: ARCMemory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An arbitrary derivation failure after Core persistence cannot expose stale custody."""
    history = TaskHistory(arc)

    def fail_sink(event: Any) -> Any:
        raise RuntimeError("injected highway failure after persistence")

    monkeypatch.setattr(arc, "_highway_sink", fail_sink)
    with pytest.raises(RuntimeError, match="after persistence"):
        history.record_snapshot(snapshot(), observation_id="confirmed-before-sink-error")
    assert history.requires_recovery
    assert history.task_snapshot("task_test") is None
    history.restore()
    assert history.task_snapshot("task_test") == snapshot()
    assert not history.requires_recovery


def test_revoked_grant_cannot_be_reactivated_under_its_historical_id(arc: ARCMemory) -> None:
    """Regranting requires a new authenticated relationship rather than overwriting revocation."""
    history = TaskHistory(arc)
    grant = CoordinationGrant(
        id="grant",
        granting_human="human",
        recipient_session="peer",
        target_session="owner",
        rights=("read", "observe"),
        created_at=STAMP,
    )
    history.record_grant(grant, observation_id="grant-new")
    revoked = grant.model_copy(update={"revoked_at": STAMP})
    history.record_grant(revoked, observation_id="grant-revoked")
    with pytest.raises(ValueError, match="revoked grant"):
        history.record_grant(grant, observation_id="invalid-reactivation")
    assert not history.requires_recovery


def test_terminal_outcome_cannot_be_reopened_by_a_late_owner_reply(arc: ARCMemory) -> None:
    """The writer preserves historical facts without deciding execution transitions."""
    history = TaskHistory(arc)
    running = snapshot()
    history.record_snapshot(running, observation_id="accepted")
    terminal = running.model_copy(update={"status": "completed", "terminal_at": STAMP})
    history.record_snapshot(terminal, observation_id="completed")
    with pytest.raises(ValueError, match="terminal outcome"):
        history.record_snapshot(running, observation_id="late-working-reply")
    assert history.task_snapshot(running.handle) == terminal


@pytest.mark.parametrize(
    "reply",
    [
        QuestionReply(metadata={"ignored": True}),
        QuestionReply(parts=({"type": "text", "text": "attachment"},)),
    ],
    ids=["metadata-only", "attachment-only"],
)
def test_option_only_question_cannot_complete_without_an_option(
    arc: ARCMemory, reply: QuestionReply
) -> None:
    """An unrelated payload cannot stand in for the requested user decision."""
    history = TaskHistory(arc)
    accepted = snapshot().model_copy(update={"kind": "Question", "tool": "ask_user"})
    history.record_snapshot(accepted, observation_id="question-accepted")
    commands = QuestionCommands(history)
    posted = commands.post(
        accepted,
        QuestionDefinition(
            prompt="Pick one",
            kind="choice",
            allow_freeform=False,
            options=(QuestionOption(label="First", value="first"),),
        ),
    )
    with pytest.raises(ValueError, match="offered option"):
        commands.answer(accepted.handle, reply)
    assert history.task_snapshot(accepted.handle) == posted
    answered = commands.answer(accepted.handle, QuestionReply(selected_options=("first",)))
    assert answered.status == "completed"
    assert answered.result is not None


def test_direct_child_invocation_lookup_requires_exact_parent_and_human(arc: ARCMemory) -> None:
    """A lost flow receipt resolves only a direct child of its exact same-human owner."""
    history = TaskHistory(arc)
    root = snapshot().model_copy(
        update={
            "handle": "flow-root",
            "owner": TaskOwner(
                human_id="human", session_id="parent-session", assignment_id="flow-root"
            ),
        }
    )
    child = snapshot().model_copy(
        update={
            "handle": "child",
            "owner": TaskOwner(
                human_id="human",
                session_id="child-session",
                assignment_id="child",
                parent_handle="flow-root",
            ),
            "invocation_id": "intent-1",
        }
    )
    nested = child.model_copy(
        update={
            "handle": "nested",
            "owner": TaskOwner(
                human_id="human",
                session_id="nested-session",
                assignment_id="nested",
                parent_handle="child",
            ),
        }
    )
    wrong_human = child.model_copy(
        update={
            "handle": "wrong-human",
            "owner": TaskOwner(
                human_id="other",
                session_id="other-session",
                assignment_id="wrong-human",
                parent_handle="flow-root",
            ),
        }
    )
    history.record_snapshot(root, observation_id="flow-root")
    history.record_snapshot(child, observation_id="child")
    history.record_snapshot(nested, observation_id="nested")
    history.record_snapshot(wrong_human, observation_id="wrong-human")

    found = history.direct_child_by_invocation(
        "flow-root", "intent-1", session_id="parent-session", human_id="human"
    )
    assert tuple(task.handle for task in found) == ("child",)
    assert (
        history.direct_child_by_invocation(
            "flow-root", "intent-1", session_id="wrong-session", human_id="human"
        )
        == ()
    )
    assert (
        history.direct_child_by_invocation(
            "flow-root", "intent-1", session_id="parent-session", human_id="other"
        )
        == ()
    )
