"""Highway-only events recorded behind the call, in groups (``gact.highway_writer``).

The contract: a queued event is in clio-core before it reaches the highway; a session's
events keep their order; a burst costs one put per ``_events`` chunk, not one per event;
a failed group is reported once, typed, at the session's next barrier; and a slow put
never holds the thread that queued the event (the model loop, a tool call).
"""

from __future__ import annotations

import threading
import time
import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from clio_agent.arc.memory import EVENTS_SCOPE, ARCMemory
from clio_agent.arc.segment_errors import ArcPersistError
from clio_agent.gact.highway_writer import (
    HIGHWAY_RECORD_UNCONFIRMED,
    HighwayRecordUnconfirmed,
    HighwayWriter,
    defer_event,
    flush_session,
)
from clio_agent.gact.semantic_events import SemanticEvent


def _ev(event_type: str, n: int, sid: str) -> SemanticEvent:
    return SemanticEvent(
        event_type=event_type,
        session_id=sid,
        trace_id="trace_t1",
        turn_id="t1",
        payload={"n": n},
        occurred_at="2026-11-03T00:00:00+00:00",
    )


def _logged(arc: ARCMemory, sid: str) -> list[tuple[str, int]]:
    """``(event_type, payload n)`` of the session's ``_events`` log, oldest first."""
    from clio_agent.arc import lane_chunking

    rows = []
    for scope in lane_chunking.lane_scopes(arc._segments, sid, EVENTS_SCOPE):
        for segment in arc._segments.render(sid, scope):
            content = segment.content if isinstance(segment.content, dict) else {}
            payload = content.get("payload") or {}
            rows.append((content.get("event_type"), payload.get("n")))
    return rows


def _puts(arc: ARCMemory, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []
    store = arc._segments._store
    for method in ("put", "put_at", "put_many"):
        original = getattr(store, method)

        def wrapped(*args: Any, _m: str = method, _f: Any = original, **kw: Any) -> Any:
            calls.append(_m)
            return _f(*args, **kw)

        monkeypatch.setattr(store, method, wrapped)
    return calls


def test_a_burst_is_recorded_in_order_in_one_group_before_it_reaches_the_highway(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Forty queued events: in order in ``_events``, one put, highway after the record.

    SABOTAGE: call the sink before ``group.flush()`` in ``HighwayWriter._record`` and the
    sink sees an event clio-core does not hold yet (``held`` is False).
    """
    arc = ARCMemory(data_dir=str(tmp_path / "arc"))
    sid = f"sess_hw_burst_{uuid.uuid4().hex[:8]}"
    arc._segments.append(sid, EVENTS_SCOPE, "semantic_event", {"warm": True})
    held: list[bool] = []

    def sink(event: SemanticEvent) -> dict[str, Any]:
        arc._segments.release(sid)  # read clio-core's record, not this process's cache
        logged = [row for row in _logged(arc, sid) if row[0]]
        held.append((event.event_type, event.payload["n"]) in logged)
        return {}

    arc.set_highway_sink(sink)
    writer = HighwayWriter(arc)
    gate = threading.Event()
    monkeypatch.setattr(writer, "_run", _gated(writer._run, gate))
    calls = _puts(arc, monkeypatch)
    for n in range(40):
        writer.submit(
            sid, _ev("tool.call.started" if n % 2 == 0 else "tool.call.completed", n, sid)
        )
    gate.set()
    writer.flush(sid, timeout=30)
    writer.close()

    rows = [row for row in _logged(arc, sid) if row[0]]
    assert [n for _type, n in rows] == list(range(40))
    assert held == [True] * 40
    assert len(calls) <= 2, calls  # one group: a put per chunk, not one per event


def _gated(run: Any, gate: threading.Event) -> Any:
    def gated() -> None:
        gate.wait(10)  # let the whole burst queue first, so it is one group
        run()

    return gated


def test_a_failed_group_never_reaches_the_highway_and_is_reported_once(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A lost group: no projection, a typed error at the next barrier, then a clean one.

    SABOTAGE: drop ``state.error = error`` in ``HighwayWriter._settle`` and the barrier
    returns as if the records were held.
    """
    arc = ARCMemory(data_dir=str(tmp_path / "arc"))
    sid = f"sess_hw_lost_{uuid.uuid4().hex[:8]}"
    fired: list[str] = []
    arc.set_highway_sink(lambda event: fired.append(event.event_type) or {})
    writer = HighwayWriter(arc)

    def lost(*_a: Any, **_k: Any) -> None:
        raise ArcPersistError("put not confirmed")

    monkeypatch.setattr(arc, "on_semantic_event", lost)
    writer.submit(sid, _ev("tool.call.started", 1, sid))
    with pytest.raises(HighwayRecordUnconfirmed) as raised:
        writer.flush(sid, timeout=30)
    assert raised.value.reason == HIGHWAY_RECORD_UNCONFIRMED
    assert fired == []
    writer.flush(sid, timeout=30)  # reported once
    writer.close()


def test_a_slow_put_never_holds_the_thread_that_queued_the_event(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """While the writer waits on clio-core, queueing returns at once.

    SABOTAGE: record inline in ``HighwayWriter.submit`` (``self._record([...])``) and the
    second submit waits for the slow put.
    """
    arc = ARCMemory(data_dir=str(tmp_path / "arc"))
    sid = f"sess_hw_slow_{uuid.uuid4().hex[:8]}"
    release = threading.Event()
    original = arc.on_semantic_event

    def slow(event: Any) -> None:
        release.wait(10)
        original(event)

    monkeypatch.setattr(arc, "on_semantic_event", slow)
    writer = HighwayWriter(arc)
    started = time.perf_counter()
    writer.submit(sid, _ev("lm.call", 1, sid))
    writer.submit(sid, _ev("lm.call", 2, sid))
    queued = time.perf_counter() - started
    with pytest.raises(TimeoutError):
        writer.flush(sid, timeout=0.2)
    release.set()
    writer.flush(sid, timeout=30)
    writer.close()
    assert queued < 0.5
    assert [n for _t, n in _logged(arc, sid) if _t] == [1, 2]


def test_only_highway_only_types_are_deferred(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``turn.completed`` and friends are recorded on the caller; the switch turns it off.

    SABOTAGE: drop the ``DEFERRED_EVENT_TYPES`` check in ``defer_event`` and a turn event
    is queued.
    """
    arc = ARCMemory(data_dir=str(tmp_path / "arc"))
    sid = f"sess_hw_types_{uuid.uuid4().hex[:8]}"
    app = SimpleNamespace(state=SimpleNamespace())
    assert defer_event(app, arc, sid, _ev("turn.completed", 1, sid), {}) is False
    assert defer_event(app, arc, "", _ev("tool.call.started", 1, sid), {}) is False
    monkeypatch.setenv("CLIO_ARC_HIGHWAY_DEFERRED", "0")
    assert defer_event(app, arc, sid, _ev("tool.call.started", 1, sid), {}) is False
    monkeypatch.setenv("CLIO_ARC_HIGHWAY_DEFERRED", "1")
    assert defer_event(app, arc, sid, _ev("tool.call.started", 2, sid), {}) is True
    flush_session(app, sid)
    app.state.highway_writer.close()
    assert [n for _t, n in _logged(arc, sid) if _t] == [2]


def test_an_artifact_event_confirms_its_index_record_after_clio_core_holds_it(
    tmp_path,
) -> None:
    """The ``artifact.*`` confirm runs after the record, as on the synchronous path.

    SABOTAGE: pass ``after=None`` for artifact events in ``defer_event`` and the
    registry's pending entry is never confirmed.
    """
    arc = ARCMemory(data_dir=str(tmp_path / "arc"))
    sid = f"sess_hw_artifact_{uuid.uuid4().hex[:8]}"
    confirmed: list[tuple[str, bool]] = []

    def confirm(event_id: str) -> None:
        arc._segments.release(sid)
        confirmed.append((event_id, bool([r for r in _logged(arc, sid) if r[0]])))

    app = SimpleNamespace(
        state=SimpleNamespace(artifact_registry=SimpleNamespace(confirm_event=confirm))
    )
    payload = {"event_id": "evt_1"}
    assert defer_event(app, arc, sid, _ev("artifact.transform.recorded", 1, sid), payload) is True
    flush_session(app, sid)
    app.state.highway_writer.close()
    assert confirmed == [("evt_1", True)]


def test_a_synchronous_event_is_recorded_after_what_its_session_queued(tmp_path) -> None:
    """A turn event after a queued ``lm.call`` lands after it: the log keeps emission order.

    SABOTAGE: drop the ``flush_session`` in ``defer_event``'s non-deferred branch and the
    turn event is recorded before the queued model call.
    """
    arc = ARCMemory(data_dir=str(tmp_path / "arc"))
    sid = f"sess_hw_order_{uuid.uuid4().hex[:8]}"
    app = SimpleNamespace(state=SimpleNamespace())
    release = threading.Event()
    original = arc.on_semantic_event

    def slow(event: Any) -> None:
        if event.event_type == "lm.call":
            release.wait(10)
        original(event)

    arc.on_semantic_event = slow  # type: ignore[method-assign]
    assert defer_event(app, arc, sid, _ev("lm.call", 1, sid), {}) is True
    threading.Timer(0.3, release.set).start()
    turn_event = _ev("react.step.completed", 2, sid)
    assert defer_event(app, arc, sid, turn_event, {}) is False  # waited for the queue
    original(turn_event)  # the caller's synchronous record
    app.state.highway_writer.close()
    assert [row for row in _logged(arc, sid) if row[0]] == [
        ("lm.call", 1),
        ("react.step.completed", 2),
    ]


def test_a_queued_store_write_lands_before_the_event_queued_after_it(tmp_path) -> None:
    """The artifact index put runs before the event that confirms it is recorded.

    SABOTAGE: run operations after ``group.flush()`` in ``HighwayWriter._record`` and the
    operation sees its confirming event already in clio-core.
    """
    arc = ARCMemory(data_dir=str(tmp_path / "arc"))
    sid = f"sess_hw_op_{uuid.uuid4().hex[:8]}"
    seen: list[list[tuple[str, int]]] = []

    def put() -> None:
        arc._segments.release(sid)
        seen.append([row for row in _logged(arc, sid) if row[0]])

    writer = HighwayWriter(arc)
    writer.submit_operation(sid, put)
    writer.submit(sid, _ev("artifact.transform.recorded", 1, sid))
    writer.flush(sid, timeout=30)
    writer.close()
    assert seen == [[]]
    assert [row for row in _logged(arc, sid) if row[0]] == [("artifact.transform.recorded", 1)]
