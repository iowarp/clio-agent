"""ARC is the SOURCE of the data highway (the inversion).

Every semantic event flows through ``ARCMemory.record_semantic_event`` FIRST:
ARC PERSISTS the event (one ``semantic_event`` segment under the reserved
``_events`` scope), FOLDS the observer (``_live`` scope), and then DERIVES the
highway via the injected sink. These tests guard that contract end to end:

* (a) a normal event is persisted + folded, and the highway sink fires once.
* (b) the high-volume ``lm.token.delta`` stream is NOT persisted to ``_events``.
* (c) the reserved ``_events`` / ``_live`` scopes are INVISIBLE to an expert
      scope's working-set render + trajectory keys (they never reach a prompt).
* (d) with no highway sink wired, record still persists + folds and returns {}.
* (e) the fallback path (arc without record / arc None) fans out via sink.emit.
"""

from __future__ import annotations

from typing import Any

from clio_agent.arc.live import LIVE_SCOPE
from clio_agent.arc.memory import EVENTS_SCOPE, ARCMemory
from clio_agent.gact.events import EventBus
from clio_agent.gact.semantic_events import (
    NoopSemanticTraceBackend,
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


def _turn_started(**kw: Any) -> SemanticEvent:
    return _ev("turn.started", payload={"input": "stations near San Diego"}, **kw)


# --- (a) record persists + folds + highway fires exactly once ---------------


def test_record_persists_segment_folds_observer_and_fires_highway_once(tmp_path):
    """The inversion: one event -> one persisted ``semantic_event`` segment under
    ``_events`` AND an observer fold AND the highway sink fires exactly once."""
    arc = ARCMemory(data_dir=str(tmp_path / "arc"))
    fired: list[SemanticEvent] = []
    arc.set_highway_sink(lambda e: fired.append(e) or {"derived": True})

    event = _turn_started()
    result = arc.record_semantic_event(event)

    # (1) persisted as a semantic_event segment under the reserved _events scope.
    segs = arc.render_segments("s1", EVENTS_SCOPE)
    assert len(segs) == 1
    seg = segs[0]
    assert seg.kind == "semantic_event"
    assert seg.turn_id == "t1"
    assert seg.content["event_type"] == "turn.started"
    assert seg.content["payload"]["input"] == "stations near San Diego"

    # (2) observer folded it (the _live scope now holds the turn).
    view = arc.get_live_context("s1")
    assert view["turns"][0]["question"] == "stations near San Diego"

    # (3) the highway sink fired exactly once, with the SAME raw event, and its
    #     return value is what record_semantic_event returns (the derivation).
    assert fired == [event]
    assert result == {"derived": True}


def test_record_through_real_sink_no_recursion(tmp_path):
    """record -> sink.emit (real SemanticEventSink) terminates: persisting the
    semantic_event segment fires the op-logger (arc.op), which is recorded again
    but SKIPPED from persistence, so there is no unbounded re-entry."""
    arc = ARCMemory(data_dir=str(tmp_path / "arc"))
    sink = SemanticEventSink(
        bus=EventBus(),
        trace_backend=NoopSemanticTraceBackend(),
        live_consumers=None,  # ARC is the source; the sink has NO arc consumer
    )
    # Wire the op-logger so an _events append emits an arc.op back through record
    # (the exact path that recursed before arc.op was added to _EVENT_LOG_SKIP).
    arc.set_segment_op_logger(
        lambda op, session_id, scope, **kw: arc.record_semantic_event(
            _ev("arc.op", status=op, payload={"op": op, "scope": scope})
        )
    )
    arc.set_highway_sink(sink.emit)

    full = arc.record_semantic_event(_turn_started())

    # Terminated (no RecursionError) and produced the full projected dict.
    assert full["event_type"] == "turn.started"
    # Exactly ONE persisted semantic_event (the turn.started); every arc.op the
    # op-logger emitted was skipped from _events.
    segs = arc.render_segments("s1", EVENTS_SCOPE)
    assert [s.content["event_type"] for s in segs] == ["turn.started"]


# --- (b) lm.token.delta is highway-only, never persisted --------------------


def test_token_delta_is_not_persisted_to_events(tmp_path):
    """The high-volume transient token stream rides the highway only."""
    arc = ARCMemory(data_dir=str(tmp_path / "arc"))
    fired: list[SemanticEvent] = []
    arc.set_highway_sink(lambda e: fired.append(e) or {})

    arc.record_semantic_event(_ev("lm.token.delta", payload={"delta": "hel"}))
    arc.record_semantic_event(_ev("lm.token.delta", payload={"delta": "lo"}))

    # Not persisted as segments...
    assert arc.render_segments("s1", EVENTS_SCOPE) == []
    # ...but STILL derived onto the highway (it is a live-stream event).
    assert [e.event_type for e in fired] == ["lm.token.delta", "lm.token.delta"]


# --- (c) the reserved scopes never reach an expert prompt -------------------


def test_expert_scope_render_excludes_events_and_live(tmp_path):
    """An expert scope's working-set render + trajectory keys EXCLUDE both reserved
    scopes — the persisted event log + observer fold can never enter a model prompt."""
    arc = ARCMemory(data_dir=str(tmp_path / "arc"))
    # Record events: writes to BOTH _events (persist) and _live (observer fold).
    arc.record_semantic_event(_turn_started())
    arc.record_semantic_event(
        _ev(
            "expert.response.completed",
            actor={"agent_id": "data"},
            payload={"answer": "Found 71 stations.", "reasoning": "x"},
        )
    )
    # The expert writes its own working-set segments to its OWN scope.
    arc.append_segment("s1", "agentA", "thought", {"text": "T0"}, step=0)
    arc.append_segment("s1", "agentA", "observation", {"text": "O0"}, step=0)

    # The reserved scopes really do hold content (so the exclusion is meaningful).
    assert arc.render_segments("s1", EVENTS_SCOPE)
    assert arc.render_segments("s1", LIVE_SCOPE)

    # Working-set render of the expert scope sees ONLY the expert's own kinds.
    ws = arc.render_working_set("s1", "agentA")
    assert {s.kind for s in ws} == {"thought", "observation"}
    assert all(s.scope == "agentA" for s in ws)
    assert all(s.kind not in {"semantic_event", "turn_event"} for s in ws)

    # The trajectory projection (the model prompt) carries neither reserved scope.
    keys = arc.render_segments_keys("s1", "agentA")
    assert keys == {"thought_0": "T0", "observation_0": "O0"}

    # And rendering the reserved scopes' OWN keys yields nothing the prompt models
    # (semantic_event / turn_event are not in segments_to_keys' allowlist).
    assert arc.render_segments_keys("s1", EVENTS_SCOPE) == {}
    assert arc.render_segments_keys("s1", LIVE_SCOPE) == {}


# --- (d) no highway sink: still persists + folds, returns {} ----------------


def test_record_without_highway_sink_persists_folds_returns_empty(tmp_path):
    """No sink wired (memory-only / pre-wire) -> record still persists + folds and
    returns {} without crashing (the highway is simply not derived yet)."""
    arc = ARCMemory(data_dir=str(tmp_path / "arc"))
    assert arc._highway_sink is None  # not wired

    result = arc.record_semantic_event(_turn_started())

    assert result == {}
    assert len(arc.render_segments("s1", EVENTS_SCOPE)) == 1
    assert arc.get_live_context("s1")["turns"][0]["question"] == "stations near San Diego"


def test_record_with_empty_session_or_unset_type_is_noop(tmp_path):
    """An event with no session_id / event_type persists nothing (guarded)."""
    arc = ARCMemory(data_dir=str(tmp_path / "arc"))
    arc.set_highway_sink(lambda e: {})
    arc.record_semantic_event(_ev("turn.started", sid=""))  # no session
    arc.record_semantic_event(_ev(""))  # no event type
    assert arc.render_segments("s1", EVENTS_SCOPE) == []


# --- (e) fallback fan-out via sink.emit (arc without record / None) ---------


def test_fallback_to_sink_emit_when_arc_has_no_record():
    """The _emit_semantic_event fallback: an object that is NOT an ARCMemory (no
    record_semantic_event) means the highway fans out via sink.emit directly —
    mirroring the build-time / memory-disabled path."""
    sink = SemanticEventSink(bus=EventBus(), trace_backend=NoopSemanticTraceBackend())
    event = _turn_started()

    # This is the exact expression _emit_semantic_event evaluates.
    class _ArcNoRecord:
        pass

    for arc in (_ArcNoRecord(), None):
        rec = getattr(arc, "record_semantic_event", None)
        full = rec(event) if rec is not None else sink.emit(event)
        assert full["event_type"] == "turn.started"


def test_release_drops_events_scope(tmp_path):
    """release_session erases the reserved _events scope (idle -> baseline)."""
    arc = ARCMemory(data_dir=str(tmp_path / "arc"))
    arc.set_highway_sink(lambda e: {})
    arc.record_semantic_event(_turn_started())
    assert arc.render_segments("s1", EVENTS_SCOPE)  # holds the persisted event

    arc.release_session("s1")
    assert arc.render_segments("s1", EVENTS_SCOPE) == []


def test_flush_and_release_drops_events_scope(tmp_path):
    """flush_and_release erases the _events scope across all sessions."""
    arc = ARCMemory(data_dir=str(tmp_path / "arc"))
    arc.set_highway_sink(lambda e: {})
    arc.record_semantic_event(_turn_started())
    assert arc.render_segments("s1", EVENTS_SCOPE)

    arc.flush_and_release()
    assert arc.render_segments("s1", EVENTS_SCOPE) == []
