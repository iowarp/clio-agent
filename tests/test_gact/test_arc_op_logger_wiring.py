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

import types

from clio_agent.arc.memory import ARCMemory
from clio_agent.gact.app import _wire_arc_op_logger


def _fake_app(arc):
    return types.SimpleNamespace(state=types.SimpleNamespace(arc=arc))


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
