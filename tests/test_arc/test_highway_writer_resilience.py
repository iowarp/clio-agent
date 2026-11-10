"""The highway writer survives what goes wrong in a batch, and is closed and pruned.

The contract (``gact.highway_writer``): every batch is settled whatever raised in it, so
a barrier never waits on a batch that was dropped, and the one writer thread records the
next batch; a session's bookkeeping is dropped once nothing of it is left to report; a
replaced or shut-down writer records what it holds first (bounded, typed when it could
not); the turn's publication reports a lost record instead of raising; the writer thread
runs its clio-core calls in place.
"""

from __future__ import annotations

import logging
import threading
import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from clio_agent.arc.memory import EVENTS_SCOPE, ARCMemory
from clio_agent.arc.native_threads import run_on_native_thread
from clio_agent.gact.artifacts.index_puts import INDEX_PUT_SINK, IndexPut
from clio_agent.gact.highway_writer import (
    HIGHWAY_CLOSE_UNRECORDED,
    HIGHWAY_RECORD_UNCONFIRMED,
    HighwayRecordUnconfirmed,
    HighwayWriter,
    close_app_writer,
    defer_event,
    flush_session,
    flush_session_reported,
    highway_writer,
)
from clio_agent.gact.semantic_events import SemanticEvent


def _ev(event_type: str, n: int, sid: str) -> SemanticEvent:
    return SemanticEvent(
        event_type=event_type,
        session_id=sid,
        trace_id="trace_t1",
        turn_id="t1",
        payload={"n": n},
        occurred_at="2026-11-05T00:00:00+00:00",
    )


def _logged(arc: ARCMemory, sid: str) -> list[int]:
    """Payload ``n`` of the session's ``_events`` log, oldest first."""
    from clio_agent.arc import lane_chunking

    arc._segments.release(sid)
    rows = []
    for scope in lane_chunking.lane_scopes(arc._segments, sid, EVENTS_SCOPE):
        for segment in arc._segments.render(sid, scope):
            content = segment.content if isinstance(segment.content, dict) else {}
            if content.get("event_type"):
                rows.append((content.get("payload") or {}).get("n"))
    return rows


class _LostConfirmBackend:
    """An artifact index whose multi-put clio-core does not confirm."""

    def _put_many(self, _items: list[tuple[str, bytes]]) -> None:
        raise RuntimeError("confirm put not confirmed")


def _queue_confirm() -> None:
    """An ``after`` hook that queues one index confirm (as ``confirm_artifact_event``)."""
    sink = INDEX_PUT_SINK.get()
    assert sink is not None
    sink.append(IndexPut(_LostConfirmBackend(), "artifact:t1", b"{}"))


def test_a_lost_confirm_put_is_reported_at_the_barrier_and_the_writer_records_on(
    tmp_path,
) -> None:
    """A confirm multi-put that raises: a typed error, no hang, and the next batch lands.

    SABOTAGE: move ``put_batch(confirms)`` in ``HighwayWriter._project`` out of its
    ``try`` (or drop the ``finally`` around ``_settle`` in ``_record``) and the writer
    thread dies with the batch unsettled: the barrier times out instead of raising, and
    the next event is never recorded.
    """
    arc = ARCMemory(data_dir=str(tmp_path / "arc"))
    sid = f"sess_hw_confirm_{uuid.uuid4().hex[:8]}"
    writer = HighwayWriter(arc)
    try:
        writer.submit(sid, _ev("artifact.transform.recorded", 1, sid), _queue_confirm)
        with pytest.raises(HighwayRecordUnconfirmed) as raised:
            writer.flush(sid, timeout=10)  # production waits without a timeout
        assert raised.value.reason == HIGHWAY_RECORD_UNCONFIRMED
        assert _logged(arc, sid) == [1]  # the event itself is held

        writer.submit(sid, _ev("tool.call.started", 2, sid))
        writer.flush(sid, timeout=10)
        assert _logged(arc, sid) == [1, 2]
    finally:
        writer.close()


def test_a_session_is_forgotten_once_nothing_of_it_is_pending_or_unreported(tmp_path) -> None:
    """Recorded sessions leave no entry; a lost one stays until its barrier reports it.

    SABOTAGE: drop the ``del self._sessions[sid]`` in ``HighwayWriter._settle`` and the
    writer keeps one entry per session the process ever saw.
    """
    arc = ARCMemory(data_dir=str(tmp_path / "arc"))
    writer = HighwayWriter(arc)
    try:
        sids = [f"sess_hw_prune_{n}_{uuid.uuid4().hex[:6]}" for n in range(5)]
        for n, sid in enumerate(sids):
            writer.submit(sid, _ev("lm.call", n, sid))
        for sid in sids:
            writer.flush(sid, timeout=10)
        assert writer._sessions == {}

        lost = f"sess_hw_prune_lost_{uuid.uuid4().hex[:6]}"
        writer.submit(lost, _ev("artifact.transform.recorded", 9, lost), _queue_confirm)
        with pytest.raises(HighwayRecordUnconfirmed):
            writer.flush(lost, timeout=10)
        assert writer._sessions == {}
    finally:
        writer.close()


def test_replacing_the_apps_arc_drains_the_old_writer_first(tmp_path) -> None:
    """A new ARC: the old writer's queued events reach the old ARC and its thread ends.

    SABOTAGE: drop ``writer.close(...)`` in ``highway_writer`` and the old writer's
    queued event is still pending (and its thread alive) when the new writer is made.
    """
    old_arc = ARCMemory(data_dir=str(tmp_path / "old"))
    new_arc = ARCMemory(data_dir=str(tmp_path / "new"))
    sid = f"sess_hw_swap_{uuid.uuid4().hex[:8]}"
    app = SimpleNamespace(state=SimpleNamespace())
    release = threading.Event()
    original = old_arc.on_semantic_event

    def slow(event: Any) -> None:
        release.wait(10)
        original(event)

    old_arc.on_semantic_event = slow  # type: ignore[method-assign]
    assert defer_event(app, old_arc, sid, _ev("lm.call", 1, sid), {}) is True
    old = app.state.highway_writer
    threading.Timer(0.2, release.set).start()
    new = highway_writer(app, new_arc)
    try:
        assert new is not old and app.state.highway_writer is new
        assert old._thread is not None and not old._thread.is_alive()
        assert _logged(old_arc, sid) == [1]
    finally:
        release.set()
        close_app_writer(app)


def test_shutdown_closes_the_apps_writer_bounded_and_reports_what_it_left(
    tmp_path, caplog: pytest.LogCaptureFixture
) -> None:
    """``close_app_writer`` waits at most its bound and names what was not recorded.

    SABOTAGE: drop the ``HIGHWAY_CLOSE_UNRECORDED`` warning in ``HighwayWriter.close``
    and a shutdown that left records behind says nothing.
    """
    arc = ARCMemory(data_dir=str(tmp_path / "arc"))
    sid = f"sess_hw_close_{uuid.uuid4().hex[:8]}"
    app = SimpleNamespace(state=SimpleNamespace())
    release = threading.Event()
    original = arc.on_semantic_event

    def stuck(event: Any) -> None:
        release.wait(10)
        original(event)

    arc.on_semantic_event = stuck  # type: ignore[method-assign]
    assert close_app_writer(app) == 0  # no writer yet
    assert defer_event(app, arc, sid, _ev("lm.call", 1, sid), {}) is True
    with caplog.at_level(logging.WARNING, logger="clio_agent.gact.highway_writer"):
        assert close_app_writer(app, timeout=0.2) == 1
    assert HIGHWAY_CLOSE_UNRECORDED in caplog.text
    release.set()
    app.state.highway_writer._thread.join(10)
    assert _logged(arc, sid) == [1]
    # Closed: a late event is recorded on the caller's thread, never refused.
    assert defer_event(app, arc, sid, _ev("lm.call", 2, sid), {}) is False


def test_the_turn_publication_reports_a_lost_record_and_goes_on(
    tmp_path, caplog: pytest.LogCaptureFixture
) -> None:
    """``flush_session_reported`` logs the typed reason; ``flush_session`` raises it.

    SABOTAGE: let ``flush_session_reported`` re-raise and the routing decision's lost
    record abandons the turn's transcript (``turn_finalize``).
    """
    arc = ARCMemory(data_dir=str(tmp_path / "arc"))
    sid = f"sess_hw_report_{uuid.uuid4().hex[:8]}"
    app = SimpleNamespace(state=SimpleNamespace())
    assert defer_event(app, arc, sid, _ev("lm.call", 1, sid), {})
    writer = app.state.highway_writer
    writer.submit(sid, _ev("artifact.transform.recorded", 2, sid), _queue_confirm)
    try:
        with caplog.at_level(logging.WARNING, logger="clio_agent.gact.highway_writer"):
            assert flush_session_reported(app, sid, "routing_decision") is False
        assert "step=routing_decision" in caplog.text
        assert HIGHWAY_RECORD_UNCONFIRMED in caplog.text
        assert flush_session_reported(app, sid, "highway_records") is True  # reported once

        writer.submit(sid, _ev("artifact.transform.recorded", 3, sid), _queue_confirm)
        with pytest.raises(HighwayRecordUnconfirmed):
            flush_session(app, sid)
    finally:
        writer.close()


def test_the_writer_thread_runs_its_clio_core_calls_in_place(tmp_path) -> None:
    """A native call on the writer thread is not handed to a native-pool worker.

    SABOTAGE: drop ``mark_native_thread()`` from ``HighwayWriter._run`` and the call runs
    on an ``arc-native`` worker instead of the writer thread.
    """
    arc = ARCMemory(data_dir=str(tmp_path / "arc"))
    sid = f"sess_hw_native_{uuid.uuid4().hex[:8]}"
    writer = HighwayWriter(arc)
    seen: list[tuple[threading.Thread, threading.Thread]] = []
    writer.submit_operation(
        sid,
        lambda: seen.append(
            (threading.current_thread(), run_on_native_thread(threading.current_thread))
        ),
    )
    writer.flush(sid, timeout=10)
    writer.close()
    [(writer_thread, native)] = seen
    assert writer_thread.name == "clio-highway-writer"
    assert native is writer_thread


def test_an_ordering_wait_leaves_a_lost_group_to_the_reporting_barrier(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A synchronous event after a lost one waits, does not raise, and does not consume it.

    SABOTAGE: call ``flush_session(app, session_id)`` (reporting) in ``defer_event``'s
    non-deferred branch and the turn event raises the lost ``lm.call``'s error, and the
    loop's barrier afterwards finds nothing to report.
    """
    arc = ARCMemory(data_dir=str(tmp_path / "arc"))
    sid = f"sess_hw_ordering_{uuid.uuid4().hex[:8]}"
    app = SimpleNamespace(state=SimpleNamespace())
    original = arc.on_semantic_event

    def lose_model_calls(event: Any) -> None:
        if event.event_type == "lm.call":
            raise RuntimeError("put not confirmed")
        original(event)

    monkeypatch.setattr(arc, "on_semantic_event", lose_model_calls)
    assert defer_event(app, arc, sid, _ev("lm.call", 1, sid), {}) is True
    try:
        assert defer_event(app, arc, sid, _ev("react.step.completed", 2, sid), {}) is False
        with pytest.raises(HighwayRecordUnconfirmed):
            flush_session(app, sid)  # the loop's barrier reports it
        flush_session(app, sid)  # once
    finally:
        app.state.highway_writer.close()
