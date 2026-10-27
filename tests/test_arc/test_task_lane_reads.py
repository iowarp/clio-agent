"""A tree load reads no content: the lane carries what replay needs, and old stores get it once."""

from __future__ import annotations

from typing import Any

import pytest

from clio_agent.arc import lane_chunking
from clio_agent.arc.memory import ARCMemory
from clio_agent.arc.task_types import (
    AssignmentDetails,
    ContextBoundary,
    MailItem,
    RecipientInput,
    TaskOwner,
    TaskSnapshot,
)
from clio_agent.gact.task_event_lane import TASK_EVENT_LANE, migrate_task_event_lane
from clio_agent.gact.task_history import TaskHistory
from clio_agent.gact.task_lane_reads import (
    INPUT_COMMITTED,
    checkpoint_identity,
    migrate_task_lane_reads,
    reads_owed,
)

STAMP = "2026-10-27T20:00:00Z"


def _built(arc: ARCMemory) -> TaskHistory:
    """A history past its one-time index build, so trees load lazily per root."""
    history = TaskHistory(arc)
    history.restore()
    history.complete_index_build()
    return history


def _assignment(history: TaskHistory) -> TaskSnapshot:
    return TaskSnapshot(
        handle="task_owner",
        kind="Session",
        tool="conversation",
        label="Owner",
        invocation_id="fixture:owner",
        owner=TaskOwner(human_id="human", session_id="owner", assignment_id="task_owner"),
        created_at=STAMP,
        updated_at=STAMP,
        status="active",
        raw_status="active",
        description="Survey the dataset",
        details=history.write_content("owner", AssignmentDetails().model_dump(mode="json")),
    )


def _commit_input(history: TaskHistory) -> None:
    item = history.record_mail(
        MailItem(
            id="mail-1",
            recipient_session="owner",
            sender_session="sender",
            kind="peer",
            content=history.write_content("owner", {"text": "hello"}),
            created_at=STAMP,
        )
    )
    history.record_input(
        RecipientInput(
            id="input-1",
            session_id="owner",
            assignment_id="assignment",
            iteration_id="iteration",
            boundary=ContextBoundary(session_id="owner", scope="main", logical_time=0),
            notification_ids=(item.id,),
            content=(item.content,),
            committed_at=STAMP,
        )
    )


def _lane(arc: ARCMemory) -> list[dict[str, Any]]:
    return [
        segment.content
        for segment in lane_chunking.lane_segments(arc._segments, "owner", TASK_EVENT_LANE)
    ]


def _refuse_reads(arc: ARCMemory, history: TaskHistory, monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail any content read or context-scope read a tree load might make."""

    def no_content(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("a tree load read task content")

    list_segments = arc.list_segments

    def no_context(session_id: str, scope: str, **kwargs: Any) -> Any:
        if scope == "main":
            raise AssertionError("a tree load read a context scope")
        return list_segments(session_id, scope, **kwargs)

    monkeypatch.setattr(history, "_read_content", no_content)
    monkeypatch.setattr(arc, "list_segments", no_context)


def _assert_loaded(history: TaskHistory) -> None:
    task = history.task_snapshot("task_owner")
    assert task is not None and task.description == "Survey the dataset"
    assert history.acknowledged_notifications("owner") == {"mail-1"}


def test_writer_records_annotations_and_receipts_so_loads_read_no_content(
    arc: ARCMemory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Description, details workflow and committed inputs come from the lane alone."""
    history = _built(arc)
    history.record_snapshot(_assignment(history), observation_id="owner")
    _commit_input(history)
    rows = _lane(arc)
    observed = [row for row in rows if row["event_type"] == "task.observed"]
    assert observed[0]["payload"]["lane"] == {
        "description": "Survey the dataset",
        "workflow": {"reference": None},
    }
    assert [row["event_type"] for row in rows].count(INPUT_COMMITTED) == 1
    # Receipts are lane-only: the canonical log never carries them.
    logged = [s.content["event_type"] for s in arc._live.iter_session_event_segments("owner")]
    assert INPUT_COMMITTED not in logged

    restored = TaskHistory(arc)
    restored.restore()
    _refuse_reads(arc, restored, monkeypatch)
    _assert_loaded(restored)


def test_one_time_pass_annotates_an_older_lane_and_then_skips(
    arc: ARCMemory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A lane copied from the log (no annotations, no receipts) gets them once."""
    history = _built(arc)
    history.record_snapshot(_assignment(history), observation_id="owner")
    _commit_input(history)
    migrate_task_event_lane(history)  # rebuild from the log: drops both, owes the pass
    assert not any("lane" in row["payload"] for row in _lane(arc))
    assert INPUT_COMMITTED not in [row["event_type"] for row in _lane(arc)]

    older = TaskHistory(arc)
    assert reads_owed(older)
    report = migrate_task_lane_reads(older)
    assert report["annotated"] == 1 and report["receipts"] == 1
    assert report["unreadable"] == 0
    assert not reads_owed(older)  # the marker is written: the next start skips it

    again = migrate_task_lane_reads(older)  # idempotent: nothing left to add
    assert again["annotated"] == 0 and again["receipts"] == 0

    older.restore()
    _refuse_reads(arc, older, monkeypatch)
    _assert_loaded(older)


def test_pass_requires_the_lane_copy_first(arc: ARCMemory) -> None:
    """The pass refuses, typed, on a store whose lane copy is still owed."""
    history = TaskHistory(arc)
    with pytest.raises(RuntimeError, match="task_event_lane_migration_owed"):
        migrate_task_lane_reads(history)


def test_checkpoint_identity_keeps_only_named_owner_fields() -> None:
    """An absent owner field stays absent (meaning this owner); an incomplete body says so."""
    body = {"id": "e", "invocation_id": "i", "declaration": "d", "request": "r"}
    identity = checkpoint_identity(body)
    assert identity["complete"] and "root_handle" not in identity
    assert checkpoint_identity({**body, "root_handle": None})["root_handle"] is None
    assert checkpoint_identity({"id": "e"}) == {"complete": False}
