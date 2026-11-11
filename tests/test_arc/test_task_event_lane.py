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


def _as_before_the_lane(arc: ARCMemory, session_id: str) -> None:
    """Make ``session_id`` a store from before the lane: its task events only in ``_events``.

    The lane is now a task event's one record, so the old layout is rebuilt from it:
    each lane event is appended to the whole log as the old writer did, then the lane
    is dropped.
    """
    from clio_agent.gact.semantic_events import semantic_event_from_events_content

    for segment in lane_chunking.lane_segments(arc._segments, session_id, TASK_EVENT_LANE):
        if segment.content["event_type"] == "session.input.committed":
            continue  # lane-only from the start: never in the ``_events`` log
        content = dict(segment.content)
        content["payload"] = {
            k: v for k, v in content["payload"].items() if k != task_event_lane.LANE_KEY
        }
        arc.on_semantic_event(
            semantic_event_from_events_content(content, session_id=session_id, turn_id="")
        )
    lane_chunking.drop_lane(arc._segments, session_id, TASK_EVENT_LANE)


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
    # The lane is their one record: the ``_events`` log does not repeat them, and the
    # whole log a reader sees (trace, export) merges the lane in.
    from clio_agent.gact.task_event_merge import session_log_segments

    logged = [s.content["event_type"] for s in arc._live.iter_session_event_segments("owner")]
    assert "session.mail.accepted" not in logged and "lm.call" in logged
    whole = [s.content["event_type"] for s in session_log_segments(arc, "owner")]
    assert whole.count("session.mail.accepted") == 3 and "lm.call" in whole

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
    _as_before_the_lane(arc, "owner")
    assert _lane_types(arc, "owner") == []

    older = TaskHistory(arc)
    assert migration_owed(older)
    report = migrate_task_event_lane(older)
    assert report is not None and report["events"] == 4
    assert report["sessions_with_task_events"] == 1
    assert older.index.done_marker(LANE_DONE) is not None
    assert not migration_owed(older)
    assert _lane_types(arc, "owner") == ["session.mail.accepted"] * 4  # no lm.call
    older.restore()
    assert not older.index_build_pending  # trees load from the lane, not a full replay
    assert [item.id for item in older.pending_mail("owner")] == expected

    assert not migration_owed(older)  # the second start only reads the marker


def test_rerun_after_interruption_rebuilds_without_duplicates(
    arc: ARCMemory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A copy interrupted before its marker reruns from the log, not on top of itself."""
    history = TaskHistory(arc)
    for index in range(2):
        history.record_mail(_mail(history, str(index)))
    _as_before_the_lane(arc, "owner")
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


def test_a_migration_rerun_keeps_task_events_only_the_lane_holds(arc: ARCMemory) -> None:
    """A copy rerun (a version bump, an interrupted first run) keeps lane-only events.

    SABOTAGE: read only the ``_events`` scopes in ``_copy_session`` and the rerun drops
    the events written since the lane became their one record.
    """
    history = TaskHistory(arc)
    history.record_mail(_mail(history, "old"))
    _as_before_the_lane(arc, "owner")
    assert migrate_task_event_lane(history)["events"] == 1
    history.record_mail(_mail(history, "new"))  # lane only
    history.index.put_done_marker(LANE_DONE, {"version": 0})  # owed again
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


def test_the_provenance_timeline_reads_the_lane_too(arc: ARCMemory) -> None:
    """The native provenance timeline is a reader of the whole log: task events, whose
    one record is the lane, are in it. SABOTAGE: read ``iter_session_event_segments``
    alone in ``_native_events`` and the mail events are missing."""
    from types import SimpleNamespace

    from clio_agent.gact.routes.provenance import _native_events

    history = _built(arc)
    _lm_call(arc, "owner")
    history.record_mail(_mail(history, "0"))
    app = SimpleNamespace(state=SimpleNamespace(arc=arc))
    events, degradations = _native_events(app, ["owner"])  # type: ignore[arg-type]
    types = [event["event_type"] for event in events]
    assert degradations == []
    assert "session.mail.accepted" in types and "lm.call" in types


def test_a_store_written_by_v0951_fails_the_copy_typed_and_stays_owed(arc: ARCMemory) -> None:
    """A store from v0.9.5.1 or older holds its ``_events`` log in the retired base64
    frame (a whole framed put of ``encode_segments``). The copy does not read it: it
    fails typed ``arc_chunk_format_unsupported`` (reset the CLIO state) and writes no
    marker, so an upgraded store never passes as migrated with its task events lost.

    SABOTAGE: read a record without the chunk magic as an empty chunk in
    ``chunk_records.parse_chunk`` and the copy writes its marker over zero events.
    """
    from clio_agent.arc.chunk_records import CHUNK_FORMAT_UNSUPPORTED, read_chunk
    from clio_agent.arc.schema import encode_segments
    from clio_agent.errors import ClioError

    history = TaskHistory(arc)
    history.record_mail(_mail(history, "old"))
    _as_before_the_lane(arc, "owner")
    raw = arc._segments._store
    name = arc._segments._record_name("owner", "_events")
    parsed = read_chunk(raw, "segments", name)
    assert parsed is not None and parsed.segments
    raw.put("segments", name, encode_segments(parsed.segments))  # as v0.9.5.1 stored it

    older = TaskHistory(ARCMemory(store=raw))
    with pytest.raises(ClioError) as failure:
        migrate_task_event_lane(older)
    assert failure.value.error_type == CHUNK_FORMAT_UNSUPPORTED
    assert migration_owed(older)
