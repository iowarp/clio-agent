"""Confirmed input lookup stays bounded and rebuilds from the Core context lane."""

from __future__ import annotations

from typing import Any, Never

import pytest

from clio_agent.arc.memory import ARCMemory
from clio_agent.arc.task_types import ContextBoundary, MailItem, RecipientInput
from clio_agent.gact import task_history_projection
from clio_agent.gact.task_history import TaskHistory


class NoInputScan(dict[tuple[str, str], RecipientInput]):
    """Reject a retained-history scan while permitting direct indexed reads."""

    def values(self) -> Never:
        raise AssertionError("A latest-input read scanned unrelated retained inputs")


def commit(
    history: TaskHistory, identity: str, stamp: str, *, assignment: str = "parent", epoch: str = ""
) -> RecipientInput:
    """Use the confirmed Core mailbox/input boundary, without a model or execution owner."""
    item = history.record_mail(
        MailItem(
            id=identity,
            recipient_session="owner",
            sender_human="human",
            kind="human",
            content=history.write_content("owner", {"text": identity}),
            created_at=stamp,
        )
    )
    return history.record_input(
        RecipientInput(
            id="input:" + identity,
            session_id="owner",
            assignment_id=assignment,
            iteration_id=identity,
            boundary=ContextBoundary(
                session_id="owner",
                scope="main" + ("#epoch:" + epoch if epoch else ""),
                logical_time=0,
            ),
            notification_ids=(item.id,),
            content=(item.content,),
            committed_at=stamp,
        )
    )


def test_latest_input_preserves_epoch_timestamp_and_tie_order_after_restore(arc: ARCMemory) -> None:
    history = TaskHistory(arc)
    first = commit(history, "first", "2026-10-15T12:00:00Z")
    newest = commit(history, "new", "2026-10-15T14:00:00Z", epoch="new")
    commit(history, "older", "2026-10-15T13:00:00Z", epoch="new")
    commit(history, "tie", "2026-10-15T14:00:00Z", epoch="new")
    unrelated = commit(history, "other", "2026-10-15T15:00:00Z", assignment="other")
    restored = TaskHistory(arc)
    restored.restore()
    for owner in (history, restored):
        assert owner.latest_input("parent") == newest
        assert owner.latest_input("parent", epoch="") == first
        assert owner.latest_input("parent", epoch="new") == newest
        assert owner.latest_input("parent", epoch="missing") is None
        assert owner.latest_input("other") == unrelated
        state = owner._session_state("owner")
        state._inputs = NoInputScan(state._inputs)
        assert owner.latest_input("parent") == newest
        assert owner.latest_input("absent") is None


def test_input_commit_fault_does_not_publish_unconfirmed_projection(
    arc: ARCMemory, monkeypatch: pytest.MonkeyPatch
) -> None:
    history = TaskHistory(arc)
    first = commit(history, "first", "2026-10-15T12:00:00Z")

    accept = task_history_projection._accept_input

    def crash(owner: TaskHistory, record: RecipientInput) -> None:
        if getattr(owner, "history", owner) is history:
            raise OSError("lost reply after durable input commit")
        accept(owner, record)

    monkeypatch.setattr(task_history_projection, "_accept_input", crash)
    with pytest.raises(OSError, match="lost reply"):
        commit(history, "second", "2026-10-15T13:00:00Z")
    assert history.latest_input("parent") == first
    assert history.requires_recovery
    assert history.committed_notification_input("owner", "second") is None
    recovered = TaskHistory(arc)
    recovered.restore()
    latest = recovered.latest_input("parent")
    assert latest is not None and latest.id == "input:second"
    assert recovered.pending_mail("owner") == ()


def test_failed_projection_restore_keeps_previous_confirmed_lookup(
    arc: ARCMemory, monkeypatch: pytest.MonkeyPatch
) -> None:
    history = TaskHistory(arc)
    latest = commit(history, "first", "2026-10-15T12:00:00Z")

    original = task_history_projection._scope_inputs

    def fail_context(selected: TaskHistory, *args: Any, **kwargs: Any) -> Any:
        if selected is not history:
            raise OSError("canonical input read unavailable")
        return original(selected, *args, **kwargs)

    monkeypatch.setattr(task_history_projection, "_scope_inputs", fail_context)
    with pytest.raises(OSError, match="canonical input read"):
        history.restore()
    assert history.requires_recovery
    assert history.latest_input("parent") == latest
