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

    tree = ast.parse(Path(app_mod.__file__).read_text())
    sites: list[int] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            sites += [node.lineno for tgt in node.targets if _is_app_state_arc(tgt)]
        elif isinstance(node, ast.AnnAssign) and _is_app_state_arc(node.target):
            sites.append(node.lineno)
    assert len(sites) == 1, (
        f"found {len(sites)} `app.state.arc = ...` assignments in app.py at lines {sites}; "
        f"route every arc swap through _set_app_arc(app, arc) so arc.op is always wired"
    )
    # ...and that single assignment must live inside _set_app_arc.
    lines, start = inspect.getsourcelines(_set_app_arc)
    assert start <= sites[0] < start + len(lines), (
        f"the lone app.state.arc assignment (line {sites[0]}) is not inside _set_app_arc "
        f"(lines {start}..{start + len(lines)})"
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
