"""REGRESSION GUARD (was HYPOTHESIS B repro): the re-entrancy guard is GONE, and the
split it created is now structurally impossible.

Observed live (qwopus, sess_95484029f6f9): the durable trace held ``lm.call``,
``react.step.completed`` and ``tool.call`` events, but ARC's ``_events`` store held
ONLY the orchestration events. The trace is supposed to be DERIVED from ARC, so an
event in the trace but absent from ``_events`` is the bug.

Hypothesis B pinned ONE vehicle for that split: ``record_semantic_event`` fired the
highway sink UNCONDITIONALLY but gated the ``_events`` persist behind a thread-local
re-entrancy guard (``_recording_event_tl``). Whenever a second legit record nested
inside the first event's append on the same thread, the guard short-circuited the
persist while the trace still got the event. That guard existed only to break the
``arc.op`` circularity (record -> op-logger -> arc.op -> record -> op-logger ...).

THE FIX de-circularized ``arc.op``: the gact op-logger (``_emit_arc_op``) now derives
arc.op DIRECTLY to the durable trace + SSE bus, NEVER back through
``record_semantic_event``. With no path back into record, the circularity cannot form,
so the guard was REMOVED. These tests guard that:

* the guard attribute / skip entry no longer exist,
* a record always persists to ``_events`` (no nested-record drop),
* arc.op is never persisted to ``_events`` (it never enters record at all).

Offline + deterministic: no LM Studio, no network.
"""

from __future__ import annotations

from typing import Any

from clio_agent.arc import memory as memory_mod
from clio_agent.arc.memory import EVENTS_SCOPE, ARCMemory
from clio_agent.gact.events import EventBus
from clio_agent.gact.semantic_events import (
    SemanticEvent,
    SemanticEventSink,
)


def _ev(event_type: str, *, sid: str = "s1", turn: str = "t1", **kw: Any) -> SemanticEvent:
    return SemanticEvent(
        event_type=event_type,
        session_id=sid,
        trace_id=f"trace_{turn}",
        turn_id=turn,
        occurred_at="2026-06-14T00:00:00+00:00",
        **kw,
    )


class _RecordingTraceBackend:
    """A real durable-trace backend that records exactly what the trace would hold."""

    name = "recording"

    def __init__(self) -> None:
        self.trace: list[str] = []

    def emit(self, event: SemanticEvent) -> None:
        self.trace.append(event.event_type)


def _events_in_arc(arc: ARCMemory, sid: str = "s1") -> list[str]:
    return [s.content["event_type"] for s in arc.render_segments(sid, EVENTS_SCOPE)]


# ---------------------------------------------------------------------------
# (1) The re-entrancy guard is GONE. The attribute no longer exists; arc.op is no
#     longer skip-listed (it never reaches record). The split's whole vehicle is gone.
# ---------------------------------------------------------------------------


def test_reentrancy_guard_and_arcop_skip_are_removed(tmp_path):
    arc = ARCMemory(data_dir=str(tmp_path / "arc"))
    # The thread-local guard that gated the persist is removed.
    assert not hasattr(arc, "_recording_event_tl")
    # arc.op is no longer propped up by the skip-set — it simply never enters record.
    assert "arc.op" not in memory_mod._EVENT_LOG_SKIP
    # The high-volume token stream is still highway-only.
    assert "lm.token.delta" in memory_mod._EVENT_LOG_SKIP


# ---------------------------------------------------------------------------
# (2) A real op-logger that DERIVES arc.op directly to the sink (the production
#     shape post-fix) does NOT re-enter record, so every legit event persists AND
#     reaches the trace — no split. (Pre-fix, an op-logger nesting a legit record
#     mid-append got the legit event dropped from _events; that path no longer exists.)
# ---------------------------------------------------------------------------


def test_direct_derive_oplogger_persists_every_legit_event_and_no_split(tmp_path):
    arc = ARCMemory(data_dir=str(tmp_path / "arc"))
    trace = _RecordingTraceBackend()
    sink = SemanticEventSink(bus=EventBus(), trace_backend=trace, live_consumers=None)
    arc.set_highway_sink(sink.emit)

    # The REAL post-fix op-logger shape (mirrors gact ``_emit_arc_op``): derive arc.op
    # DIRECTLY to the sink/trace — never back through arc.record_semantic_event.
    arc.set_segment_op_logger(
        lambda op, session_id, scope, **kw: sink.emit(
            _ev("arc.op", status=op, payload={"op": op, "scope": scope})
        )
    )

    for et in ("turn.started", "lm.call", "react.step.completed", "tool.call"):
        arc.record_semantic_event(_ev(et))

    persisted = _events_in_arc(arc)
    # Every legit event persisted to _events (no nested-record drop) ...
    assert persisted == ["turn.started", "lm.call", "react.step.completed", "tool.call"]
    # ... and every legit event also reached the trace: no trace ⊋ _events split.
    for et in ("turn.started", "lm.call", "react.step.completed", "tool.call"):
        assert et in trace.trace
    # arc.op reached the trace (derived) but was NEVER persisted to _events.
    assert "arc.op" in trace.trace
    assert "arc.op" not in persisted


# ---------------------------------------------------------------------------
# (3) Every recorded legit event reaches BOTH the trace and _events, for the exact
#     event types from the live symptom. The implication "in trace => in _events"
#     now holds (the bug was its failure).
# ---------------------------------------------------------------------------


def test_in_trace_implies_in_events_for_the_symptom_types(tmp_path):
    arc = ARCMemory(data_dir=str(tmp_path / "arc"))
    trace = _RecordingTraceBackend()
    sink = SemanticEventSink(bus=EventBus(), trace_backend=trace, live_consumers=None)
    arc.set_highway_sink(sink.emit)

    for et in ("lm.call", "react.step.completed", "tool.call"):
        arc.record_semantic_event(_ev(et))

    persisted = _events_in_arc(arc)
    for et in ("lm.call", "react.step.completed", "tool.call"):
        assert et in trace.trace  # reached the highway/trace
        assert et in persisted  # AND reached ARC's _events — no split
