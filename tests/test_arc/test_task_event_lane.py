"""Task trees read the task-event lane only; older stores get the lane once."""

from __future__ import annotations

from typing import Any

import pytest

from clio_agent.arc import lane_chunking
from clio_agent.arc.memory import ARCMemory
from clio_agent.arc.task_types import MailItem
from clio_agent.gact import task_event_lane
from clio_agent.gact.semantic_events import SemanticEvent
from clio_agent.gact.task_event_lane import (
    LANE_DONE,
    TASK_EVENT_LANE,
    ensure_task_event_lane,
    migrate_task_event_lane,
    migration_owed,
)
from clio_agent.gact.task_history import TaskHistory

STAMP = "2026-10-27T20:00:00Z"


def _mail(history: TaskHistory, identity: str) -> MailItem:
    return MailItem(
        id=identity,
        recipient_session="owner",
        sender_session="sender",
        kind="peer",
        content=history.write_content("owner", {"text": identity}),
        created_at=STAMP,
    )


def _lm_call(arc: ARCMemory, session_id: str) -> None:
    """A transcript-sized record in the same session's whole event log."""
    arc.record_semantic_event(
        SemanticEvent(
            event_type="lm.call",
            session_id=session_id,
            trace_id="t",
            span_id="s",
            payload={"prompt": "x" * 20_000},
        )
    )


def _built(arc: ARCMemory) -> TaskHistory:
    """A history past its one-time index build, so trees load lazily per root."""
    history = TaskHistory(arc)
    history.restore()
    history.complete_index_build()
    return history


def _lane_types(arc: ARCMemory, session_id: str) -> list[str]:
    return [
        str(segment.content["event_type"])
        for segment in lane_chunking.lane_segments(arc._segments, session_id, TASK_EVENT_LANE)
    ]


def test_task_events_are_written_to_their_lane_and_trees_read_only_it(
    arc: ARCMemory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A tree load never reads the session's whole event log."""
    history = _built(arc)
    _lm_call(arc, "owner")
    for index in range(3):
        history.record_mail(_mail(history, str(index)))
    assert _lane_types(arc, "owner") == ["session.mail.accepted"] * 3
    # The canonical log stays complete for its other readers.
    logged = [s.content["event_type"] for s in arc._live.iter_session_event_segments("owner")]
    assert logged.count("session.mail.accepted") == 3 and "lm.call" in logged

    def refuse(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("a task tree read the whole event log")

    monkeypatch.setattr(arc._live, "iter_session_event_segments", refuse)
    restored = TaskHistory(arc)
    restored.restore()
    assert not restored.index_build_pending
    assert [item.id for item in restored.pending_mail("owner")] == ["0", "1", "2"]


def test_migration_copies_task_events_once_and_boot_then_skips(arc: ARCMemory) -> None:
    """A store written before the lane gets it once; the marker makes later boots skip."""
    history = _built(arc)
    _lm_call(arc, "owner")
    for index in range(4):
        history.record_mail(_mail(history, str(index)))
    expected = [item.id for item in history.pending_mail("owner")]
    # Simulate a store from before the lane: task events only in the whole log.
    lane_chunking.drop_lane(arc._segments, "owner", TASK_EVENT_LANE)
    assert _lane_types(arc, "owner") == []

    older = TaskHistory(arc)
    assert migration_owed(older)
    report = ensure_task_event_lane(older)
    assert report is not None and report["events"] == 4
    assert report["sessions_with_task_events"] == 1
    assert older.index.done_marker(LANE_DONE) is not None
    assert not migration_owed(older)
    assert _lane_types(arc, "owner") == ["session.mail.accepted"] * 4  # no lm.call
    older.restore()
    assert not older.index_build_pending  # trees load from the lane, not a full replay
    assert [item.id for item in older.pending_mail("owner")] == expected

    assert ensure_task_event_lane(older) is None  # the second start skips


def test_rerun_after_interruption_rebuilds_without_duplicates(
    arc: ARCMemory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A copy interrupted before its marker reruns from the log, not on top of itself."""
    history = TaskHistory(arc)
    for index in range(2):
        history.record_mail(_mail(history, str(index)))
    put_marker = history.index.put_done_marker

    def crash(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("stopped before the marker")

    monkeypatch.setattr(history.index, "put_done_marker", crash)
    with pytest.raises(RuntimeError):
        migrate_task_event_lane(history)
    monkeypatch.setattr(history.index, "put_done_marker", put_marker)
    assert migration_owed(history)
    report = migrate_task_event_lane(history)
    assert report["events"] == 2
    assert _lane_types(arc, "owner") == ["session.mail.accepted"] * 2


def test_lane_writer_skips_non_task_events(arc: ARCMemory) -> None:
    """Only task events enter the lane; the canonical log still receives the event."""
    event = SemanticEvent(
        event_type="task.human.intervention", session_id="owner", trace_id="", span_id="i"
    )
    task_event_lane.record(arc, event)
    assert _lane_types(arc, "owner") == []
    logged = [s.content["event_type"] for s in arc._live.iter_session_event_segments("owner")]
    assert logged == ["task.human.intervention"]
