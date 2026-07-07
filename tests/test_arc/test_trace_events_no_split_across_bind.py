"""Repro: a semantic event reaches the durable TRACE but NEVER ARC's ``_events``.

Live symptom (qwopus, sess_95484029f6f9): the durable trace held ``lm.call`` /
``react.step.completed`` / ``tool.call`` events, yet ARC's ``_events`` store held
ONLY the orchestration events (turn.started, hook.*, agent.invocation.started, ...).
So those semantic events reached the highway/trace but were ABSENT from the ARC the
reader queried.

The contradiction the task poses: the durable trace is written ONLY at
``semantic_events.py:517`` (``SemanticEventSink.emit`` -> ``trace_backend.emit``), the
sink is called ONLY by the highway closure at ``app.py:777``, and that closure runs
INSIDE ``ARCMemory.record_semantic_event`` AFTER it persists+folds. So "in the trace"
SHOULD imply "record_semantic_event ran" SHOULD imply "_record_event_segment persisted
to _events". These tests find the mechanism by which that implication breaks.

This module now GUARDS THE FIX (was the Hypothesis C reproducer). The split came from
per-bind ARC churn; the fix makes ARC a per-clio-agent singleton (``ClioAgent`` adopts
an injected ARC, the gact server keeps ONE across builds/binds). The baseline +
Mechanism A document the surrounding behavior; the former Mechanism B reproducer is
inverted into a regression guard that the build path no longer churns the ARC.

Two candidate mechanisms are exercised against the REAL wiring
(``_set_app_arc`` + ``_emit_semantic_event`` + a real ``SemanticEventSink`` whose
``trace_backend`` is a process-shared capture list):

* MECHANISM A (bare/copy_context threads): an event emitted DEEP on a worker thread.
  ``_clio_trace_target`` / the react/expert emit guards resolve the app from the
  ``active_app()`` contextvar. A bare thread (dspy.Parallel ThreadPoolExecutor — no
  contextvar propagation) sees ``active_app() is None`` -> the event is DROPPED BEFORE
  it reaches EITHER the trace or ARC. So "in trace => record ran" is preserved; this
  is NOT the symptom. A ``contextvars.copy_context()`` worker DOES carry the app, so
  the event reaches BOTH. Mechanism A alone does not split trace from ARC.

* MECHANISM B (multi-ARC instance / per-bind ARC churn — THE reproducer): the durable
  trace backend + ``SemanticEventSink`` live on ``app.state`` and are SHARED across the
  process. ``_emit_semantic_event`` resolves the ARC FRESH every call as
  ``state.arc or _PROCESS_ARC``. An LM (re)bind swaps in a freshly-built ClioAgent with
  a BRAND-NEW ``ARCMemory`` (agent.py:206) and ``_set_app_arc`` repoints BOTH
  ``app.state.arc`` AND ``_PROCESS_ARC`` to it — while the SAME shared trace backend is
  kept. An event recorded on ARC#1 persists to ARC#1's ``_events`` and derives onto the
  shared trace; after the bind the reader queries ARC#2's ``_events`` -> empty. The
  trace (shared, append-only) still holds it. That is the exact split.
"""

from __future__ import annotations

import contextvars
import threading
import types
from typing import Any

import clio_agent.gact.app as app_mod
import clio_agent.gact.runtime.globals as globals_mod  # #714: live owner of _PROCESS_ARC
from clio_agent.arc.memory import EVENTS_SCOPE, ARCMemory
from clio_agent.gact import context as gctx
from clio_agent.gact.events import EventBus
from clio_agent.gact.semantic_events import (
    SemanticEvent,
    SemanticEventSink,
    SemanticTraceBackend,
)


class _CaptureTraceBackend:
    """A deterministic stand-in for ``FileSemanticTraceBackend``.

    The durable trace is a single append-only sink shared across the process (one
    writer thread drains all file backends; the ``SemanticEventSink`` lives on
    ``app.state``). This captures every event ``SemanticEventSink.emit`` forwards to
    ``trace_backend.emit`` — i.e. exactly what lands in the durable trace — with no
    file I/O, so the test is fully offline + deterministic.
    """

    name = "capture"

    def __init__(self) -> None:
        self.events: list[SemanticEvent] = []

    def emit(self, event: SemanticEvent) -> None:
        self.events.append(event)

    def close(self) -> None:  # pragma: no cover - parity with the real backend
        pass


# A static type assertion that the capture backend satisfies the backend Protocol
# (so the repro exercises the same emit path as production, not a looser shape).
_BACKEND: SemanticTraceBackend = _CaptureTraceBackend()


def _make_app(arc: ARCMemory, trace_backend: _CaptureTraceBackend) -> Any:
    """Build a minimal app with the REAL semantic sink wired, then run the REAL
    ``_set_app_arc`` so the highway-derive closure (app.py:773-781) is installed
    exactly as in production."""
    sink = SemanticEventSink(
        bus=EventBus(),
        trace_backend=trace_backend,
        live_consumers=None,  # ARC is the source; the sink carries NO arc consumer
    )
    app = types.SimpleNamespace(
        state=types.SimpleNamespace(
            semantic_event_sink=sink,
            semantic_trace_backend=trace_backend,
            arc=None,
            sessions={},
            semantic_trace_detail_level="semantic",
        )
    )
    app_mod._set_app_arc(app, arc)  # sets app.state.arc + _PROCESS_ARC + highway sink
    return app


def _trace_types(trace_backend: _CaptureTraceBackend) -> list[str]:
    # arc.op is DERIVED to the highway (write-log) but is not a semantic event we count
    # as "reached the trace" for the split; filter it for a clean comparison.
    return [e.event_type for e in trace_backend.events if e.event_type != "arc.op"]


def _events_types(arc: ARCMemory, sid: str) -> list[str]:
    return [s.content["event_type"] for s in arc.render_segments(sid, EVENTS_SCOPE)]


# ---------------------------------------------------------------------------
# Baseline: single ARC, MainThread emit -> event reaches BOTH trace and _events.
# Establishes that the implication "in trace => in _events" HOLDS in the simple case
# (so any later break is a real divergence, not a broken harness).
# ---------------------------------------------------------------------------
def test_baseline_single_arc_event_reaches_both_trace_and_events(tmp_path, monkeypatch):
    monkeypatch.setattr(globals_mod, "_PROCESS_ARC", None, raising=False)
    arc = ARCMemory(data_dir=str(tmp_path / "arc1"))
    trace = _CaptureTraceBackend()
    app = _make_app(arc, trace)

    app_mod._emit_semantic_event(app, "sess_x", "lm.call", turn_id="t1")

    assert _trace_types(trace) == ["lm.call"]
    assert _events_types(arc, "sess_x") == ["lm.call"]


# ---------------------------------------------------------------------------
# MECHANISM A: a BARE worker thread (no contextvar propagation) cannot resolve the
# app via active_app(); the DEEP emit path (lm.call / react.step) drops the event
# BEFORE it reaches either trace or ARC. So this does NOT produce the split — it is
# a clean drop. A copy_context() worker DOES carry the app and reaches BOTH.
#
# We exercise the production resolver _clio_trace_target indirectly through the same
# active_app() gate the deep emitters use.
# ---------------------------------------------------------------------------
def test_mechanismA_bare_thread_drops_before_trace_copyctx_reaches_both(tmp_path, monkeypatch):
    monkeypatch.setattr(globals_mod, "_PROCESS_ARC", None, raising=False)
    arc = ARCMemory(data_dir=str(tmp_path / "arcA"))
    trace = _CaptureTraceBackend()
    app = _make_app(arc, trace)

    # Establish the turn context on THIS thread (as the turn body does).
    gctx.set_turn_identity(app=app, session_id="sess_a", turn_id="t1", trace_id="tr1")

    # The deep emitters gate on active_app()/active_session_id() (config._clio_trace_target,
    # _emit_react_step_event, _emit_expert_lifecycle_event). Emulate that gate exactly.
    def deep_emit_react_step() -> str:
        a = gctx.active_app()
        sid = gctx.active_session_id()
        if a is None or not sid:
            return "dropped"  # event never built -> reaches NEITHER trace nor ARC
        app_mod._emit_semantic_event(a, sid, "react.step.completed", turn_id="t1")
        return "emitted"

    # (1) BARE thread — like dspy.Parallel's ThreadPoolExecutor — NO contextvar copy.
    bare_result: dict[str, str] = {}

    def _bare() -> None:
        bare_result["r"] = deep_emit_react_step()

    th = threading.Thread(target=_bare)
    th.start()
    th.join()

    assert bare_result["r"] == "dropped"
    # Dropped BEFORE the trace: implication "in trace => record ran" intact.
    assert _trace_types(trace) == []
    assert _events_types(arc, "sess_a") == []

    # (2) copy_context() worker (the executor delegation sites) DOES carry the app.
    ctx = contextvars.copy_context()
    copy_result: dict[str, str] = {}

    def _copy() -> None:
        copy_result["r"] = ctx.run(deep_emit_react_step)

    th2 = threading.Thread(target=_copy)
    th2.start()
    th2.join()

    assert copy_result["r"] == "emitted"
    # Reached BOTH — no split.
    assert _trace_types(trace) == ["react.step.completed"]
    assert _events_types(arc, "sess_a") == ["react.step.completed"]


# ---------------------------------------------------------------------------
# THE FIX (was MECHANISM B, the reproducer): ONE ARC per clio-agent — the build path
# no longer churns the ARC, so events recorded across a "bind" all land in the SAME
# ARC the reader queries. No split.
#
# Pre-fix, a freshly-built ClioAgent minted a NEW ARCMemory and _set_app_arc repointed
# app.state.arc to it, stranding prior events on the orphaned ARC while the shared trace
# kept them. Now the gact server constructs the ARC once (``_process_arc``) and injects
# it into every build (``ClioAgent(arc=...)``), so the "bind" reuses the SAME instance.
# ---------------------------------------------------------------------------
def test_one_arc_per_agent_no_split_across_bind(tmp_path, monkeypatch):
    monkeypatch.setattr(globals_mod, "_PROCESS_ARC", None, raising=False)
    trace = _CaptureTraceBackend()  # the ONE shared durable trace across the bind

    # The gact server constructs the ONE ARC up front (deferred-construct path).
    arc = ARCMemory(data_dir=str(tmp_path / "the_one_arc"))
    app = _make_app(arc, trace)
    sid = "sess_95484029f6f9"

    # Orchestration + a deep semantic event, recorded BEFORE the bind.
    app_mod._emit_semantic_event(app, sid, "turn.started", turn_id="t1")
    app_mod._emit_semantic_event(app, sid, "agent.invocation.started", turn_id="t1")
    app_mod._emit_semantic_event(app, sid, "lm.call", turn_id="t1")

    assert _events_types(arc, sid) == [
        "turn.started",
        "agent.invocation.started",
        "lm.call",
    ]

    # --- THE BIND (post-fix): a rebuilt agent REUSES the injected ARC. The gact bind
    # path injects ``_process_arc(app)`` (== the existing app.state.arc) into the new
    # ClioAgent, so agent.arc IS the same instance; _set_app_arc re-sets the SAME object.
    rebuilt_agent = types.SimpleNamespace(arc=app_mod._process_arc(app))
    assert rebuilt_agent.arc is arc  # the rebuild reused the one ARC, did not mint one
    app.state.agent = rebuilt_agent
    app_mod._set_app_arc(app, rebuilt_agent.arc)

    # The LIVE arc the reader queries is STILL the one ARC.
    live_arc: ARCMemory = app.state.arc
    assert live_arc is arc
    assert globals_mod._PROCESS_ARC is arc

    # A deep event recorded AFTER the bind (react.step / tool.call).
    app_mod._emit_semantic_event(app, sid, "react.step.completed", turn_id="t1")
    app_mod._emit_semantic_event(app, sid, "tool.call", turn_id="t1")

    trace_types = _trace_types(trace)
    live_events_types = _events_types(live_arc, sid)

    # ---- NO SPLIT: everything in the trace is ALSO in the live ARC's _events. ----
    expected = [
        "turn.started",
        "agent.invocation.started",
        "lm.call",
        "react.step.completed",
        "tool.call",
    ]
    assert trace_types == expected
    assert live_events_types == expected
    for et in ("lm.call", "react.step.completed", "tool.call"):
        assert et in trace_types
        assert et in live_events_types
