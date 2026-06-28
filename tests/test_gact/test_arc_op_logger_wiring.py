"""Regression: the ARC arc.op op-logger must be wired onto app.state.arc.

The real agent's ARCMemory is built ASYNC (``_construct_agent_async``), AFTER
``build_app`` ran with ``arc=None`` — so the build-time wiring saw no arc. If the
op-logger is not (re)wired onto the agent's arc, ARC writes still happen (the live
context plane works) but emit NO ``arc.op`` event, so the Trace/highway/interface
never see the live-context-plane writes. A real qwopus turn exhibited exactly this:
ARC segments were persisted per expert scope but ``arc.op`` count was zero.

This guards the wiring (`_wire_arc_op_logger`) the async path now calls.
"""

from __future__ import annotations

import ast
import inspect
import types
from pathlib import Path

import clio_agent.gact.app as app_mod
from clio_agent.arc.memory import ARCMemory
from clio_agent.gact.app import _set_app_arc, _wire_arc_op_logger


def _fake_app(arc):
    return types.SimpleNamespace(state=types.SimpleNamespace(arc=arc))


def test_set_app_arc_sets_and_wires():
    arc = ARCMemory(data_dir="/tmp/_arc_set_app_arc_test")
    app = _fake_app(None)
    _set_app_arc(app, arc)
    assert app.state.arc is arc
    assert arc._segments._op_logger is not None  # wired in one shot
    assert arc._highway_sink is not None  # highway-derive sink wired too


def test_set_app_arc_wires_highway_sink_reading_state_at_call_time(tmp_path):
    """ARC-as-source: _set_app_arc wires arc.set_highway_sink with a closure that
    reads app.state.semantic_event_sink at CALL time. The sink may be constructed
    AFTER _set_app_arc runs (build/async ordering), so the closure must not capture
    a value at wiring time. record_semantic_event then routes the event through the
    sink that gets attached later."""
    arc = ARCMemory(data_dir=str(tmp_path / "arc"))
    app = _fake_app(None)
    # Sink not present yet at wiring time -> closure must tolerate that and return {}.
    _set_app_arc(app, arc)
    from clio_agent.gact.semantic_events import SemanticEvent

    ev = SemanticEvent(event_type="turn.started", session_id="s1", trace_id="x", turn_id="t1")
    assert arc.record_semantic_event(ev) == {}  # no sink yet -> derives nothing

    # Attach a sink AFTER wiring; the closure picks it up at call time.
    fired = []
    app.state.semantic_event_sink = types.SimpleNamespace(
        emit=lambda e: fired.append(e) or {"ok": True}
    )
    ev2 = SemanticEvent(event_type="turn.started", session_id="s1", trace_id="x", turn_id="t2")
    assert arc.record_semantic_event(ev2) == {"ok": True}
    # The recorded event reached the highway sink (along with the arc.op meta-events
    # the op-logger derives from persisting it — those are highway-only by design).
    assert ev2 in fired
    # And the turn.started events were persisted to ARC FIRST (source-of-record),
    # both times — while every derived arc.op was SKIPPED from _events (no recursion).
    persisted = arc.render_segments("s1", "_events")
    assert [s.content["event_type"] for s in persisted] == ["turn.started", "turn.started"]


def test_no_raw_app_state_arc_assignment_outside_the_choke_point():
    """Guardrail: app.state.arc must ONLY be assigned inside _set_app_arc, so a new
    arc-swap site can't silently drop the arc.op op-logger (the bug a real qwopus
    turn exposed: ARC written per-expert but arc.op count = 0). Uses AST so docstring/
    comment mentions of the attribute don't count — only real assignment statements."""

    def _is_app_state_arc(t: ast.expr) -> bool:
        return (
            isinstance(t, ast.Attribute)
            and t.attr == "arc"
            and isinstance(t.value, ast.Attribute)
            and t.value.attr == "state"
            and isinstance(t.value.value, ast.Name)
            and t.value.value.id == "app"
        )

    def _assign_sites(path: str) -> list[int]:
        tree = ast.parse(Path(path).read_text())
        sites: list[int] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                sites += [node.lineno for tgt in node.targets if _is_app_state_arc(tgt)]
            elif isinstance(node, ast.AnnAssign) and _is_app_state_arc(node.target):
                sites.append(node.lineno)
        return sites

    # #714: the choke point ``_set_app_arc`` moved out of app.py into
    # ``runtime.globals`` (re-exported as a shim). The invariant is unchanged: the
    # lone ``app.state.arc = ...`` assignment must live inside ``_set_app_arc``, and
    # NO other module (app.py included) may assign it raw. Scan the file that now
    # OWNS ``_set_app_arc`` for the single site, and assert app.py has zero.
    choke_file = inspect.getsourcefile(_set_app_arc)
    assert choke_file is not None
    sites = _assign_sites(choke_file)
    assert len(sites) == 1, (
        f"found {len(sites)} `app.state.arc = ...` assignments in {choke_file} at lines "
        f"{sites}; route every arc swap through _set_app_arc(app, arc) so arc.op is wired"
    )
    # ...and that single assignment must live inside _set_app_arc.
    lines, start = inspect.getsourcelines(_set_app_arc)
    assert start <= sites[0] < start + len(lines), (
        f"the lone app.state.arc assignment (line {sites[0]}) is not inside _set_app_arc "
        f"(lines {start}..{start + len(lines)})"
    )
    # app.py must hold ZERO raw assignments now that the choke point lives elsewhere.
    app_sites = _assign_sites(app_mod.__file__)
    assert app_sites == [], (
        f"found raw `app.state.arc = ...` in app.py at lines {app_sites}; route every "
        f"arc swap through _set_app_arc(app, arc) (now in runtime.globals)"
    )


def test_wire_arc_op_logger_sets_segment_op_logger(tmp_path):
    arc = ARCMemory(data_dir=str(tmp_path / "arc"))
    # Fresh ARCMemory: the segment store has no op-logger yet.
    assert arc._segments._op_logger is None
    _wire_arc_op_logger(_fake_app(arc))
    # After wiring (the async-construction path), it IS set -> arc.op will fire.
    assert arc._segments._op_logger is not None
    assert callable(arc._segments._op_logger)


def test_wire_arc_op_logger_noop_when_arc_missing():
    # build_app runs with arc=None (agent built async later); wiring must no-op safely.
    _wire_arc_op_logger(_fake_app(None))  # must not raise
    _wire_arc_op_logger(types.SimpleNamespace(state=types.SimpleNamespace()))  # no .arc attr


def test_wired_logger_fires_on_segment_write(tmp_path):
    arc = ARCMemory(data_dir=str(tmp_path / "arc"))
    seen: list[tuple] = []
    # Wire a direct probe logger (same shape _emit_arc_op is invoked with) to prove
    # a real write reaches the op-logger once wired.
    arc.set_segment_op_logger(lambda op, session_id, scope, **kw: seen.append((op, scope)))
    arc.append_segment("s1", "agentA", "thought", {"text": "hi"}, step=0)
    assert seen, "a segment write did not reach the op-logger"
    assert seen[0][0] == "append" and seen[0][1] == "agentA"
