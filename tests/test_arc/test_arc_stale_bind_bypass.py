"""REGRESSION GUARD (was HYPOTHESIS A repro): ONE ARC per clio-agent across builds/binds.

Observed live (qwopus, sess_95484029f6f9): the DURABLE TRACE held lm.call/
react.step.completed/tool.call, but ARC's ``_events`` store held ONLY the
orchestration events. The mechanism: ``ClioAgent.__init__`` minted a FRESH ARCMemory
on every build, and an LM bind REBUILDS the agent -> ``_set_app_arc`` swapped
``app.state.arc`` (and ``_PROCESS_ARC``) to the new ARC-B. Events recorded on the prior
ARC-A persisted to ARC-A's ``_events`` and derived onto the SHARED durable trace; the
reader then queried the freshly-bound ARC-B (empty). trace ⊋ the ARC you read.

THE FIX: ARC is a per-clio-agent keystone — exactly ONE per process. ``ClioAgent``
now accepts an injected ARC, and the gact server constructs the ARC ONCE and re-injects
it on every build/bind. So ``app.state.arc`` is the SAME object for the whole process;
there is no per-bind ARC churn, hence no stranded events, hence no split.

These tests guard that fix at the unit level (ClioAgent reuses an injected ARC) and at
the wiring level (a sink-derived event lands in the very ARC the reader queries).
"""

from __future__ import annotations

import types
from typing import Any

from clio_agent.arc.memory import EVENTS_SCOPE, ARCMemory
from clio_agent.gact.events import EventBus
from clio_agent.gact.semantic_events import (
    SemanticEvent,
    SemanticEventSink,
)


class _RecordingTraceBackend:
    name = "recording"

    def __init__(self) -> None:
        self.events: list[SemanticEvent] = []

    def emit(self, event: SemanticEvent) -> None:
        self.events.append(event)


def _ev(event_type: str, *, sid: str = "s1", turn: str = "t1", **kw: Any) -> SemanticEvent:
    return SemanticEvent(
        event_type=event_type,
        session_id=sid,
        trace_id=f"trace_{turn}",
        turn_id=turn,
        occurred_at="2026-06-14T00:00:00+00:00",
        **kw,
    )


def _event_types_in_arc(arc: ARCMemory, sid: str = "s1") -> list[str]:
    return [s.content["event_type"] for s in arc.render_segments(sid, EVENTS_SCOPE)]


# --- (1) ClioAgent reuses an INJECTED ARC instead of minting a fresh one --------


def test_clioagent_reuses_injected_arc(tmp_path):
    """The root fix: ``ClioAgent(arc=...)`` adopts the injected ARC rather than
    constructing a new one. This is what lets the gact server keep ONE ARC across the
    initial build and every LM-bind rebuild."""
    from clio_agent.agent import ClioAgent

    arc = ARCMemory(data_dir=str(tmp_path / "the_one_arc"))
    # data_dir routes the agent's OTHER stores (LSM) into tmp so nothing leaks into cwd.
    dd = str(tmp_path / "agent_data")
    # Build two agents with the SAME injected ARC (models the initial build + a rebuild).
    agent_a = ClioAgent(verbose=False, data_dir=dd, arc=arc)
    agent_b = ClioAgent(verbose=False, data_dir=dd, arc=arc)

    assert agent_a.arc is arc
    assert agent_b.arc is arc
    # Both agents share the identical ARC object — no per-build churn.
    assert agent_a.arc is agent_b.arc


# --- (2) With ONE ARC, an event recorded then read back is present (no split) ----


def test_single_arc_event_reaches_the_arc_the_reader_queries(tmp_path):
    """An app wired to ONE ARC: every event derived through the highway sink ALSO
    lands in the ARC the reader queries. There is no stale second instance to strand
    it on, so trace and ``_events`` agree exactly — the inverse of the live symptom."""
    backend = _RecordingTraceBackend()
    sink = SemanticEventSink(bus=EventBus(), trace_backend=backend, live_consumers=None)
    arc = ARCMemory(data_dir=str(tmp_path / "arc"))
    app = types.SimpleNamespace(
        state=types.SimpleNamespace(
            semantic_event_sink=sink,
            arc=arc,
            sessions={},
            semantic_trace_detail_level="semantic",
        )
    )
    arc.set_highway_sink(
        lambda e: (
            app.state.semantic_event_sink.emit(e)
            if getattr(app.state, "semantic_event_sink", None) is not None
            else {}
        )
    )

    for et in ("turn.started", "lm.call", "react.step.completed", "tool.call"):
        app.state.arc.record_semantic_event(_ev(et, payload={}))

    trace_types = [e.event_type for e in backend.events]
    arc_types = _event_types_in_arc(app.state.arc)
    assert trace_types == ["turn.started", "lm.call", "react.step.completed", "tool.call"]
    # No split: everything in the trace is in the ARC the reader queries.
    assert arc_types == trace_types
    for et in ("lm.call", "react.step.completed", "tool.call"):
        assert et in trace_types
        assert et in arc_types


# --- (3) The gact bind path keeps app.state.arc STABLE across a rebuild ----------


def test_gact_process_arc_is_stable_across_rebuild(tmp_path, monkeypatch):
    """``_process_arc`` returns the SAME ARC on repeated calls once one exists, so the
    deferred-construct build AND the first LM bind inject the identical instance. This
    is the wiring guarantee behind 'one ARC per clio-agent'."""
    import clio_agent.gact.app as app_mod

    # _process_arc constructs the ARC at the cwd-relative default data_dir; chdir into
    # tmp + pin the local store so the test is hermetic (no .clio_agent leak into cwd).
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CLIO_ARC_STORE", "local")

    app = types.SimpleNamespace(
        state=types.SimpleNamespace(
            arc=None,
            semantic_event_sink=None,
            semantic_trace_detail_level="semantic",
            bus=EventBus(),
        )
    )
    arc1 = app_mod._process_arc(app)  # first call constructs + publishes it
    arc2 = app_mod._process_arc(app)  # subsequent calls reuse the SAME instance
    assert arc1 is arc2
    assert app.state.arc is arc1
