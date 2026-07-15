"""The pre-execution ``step_open`` breadcrumb on the crash path (caveat b, §2.8b).

Under the fold, the V2 loop writes its working-set atoms AFTER a step's tools run, so a
crash mid-step would leave nothing on the log for that step. The fold emits a
``step_open`` breadcrumb BEFORE execution so the step's opening atoms survive a crash.
The breadcrumb is EXCLUDED from every render (it must not perturb the working set), so
it is inspected on the raw content lane.

Sabotage (b): dropping the ``emit_step_open`` call makes ``test_crash_leaves_step_open``
red — the breadcrumb is then absent from the log after a crash.
"""

from __future__ import annotations

import uuid

import dspy
import pytest
from dspy.utils.dummies import DummyLM

import clio_agent.gact.agents.runtime as runtime
from clio_agent.arc.live import _MemoryStore
from clio_agent.arc.memory import ARCMemory
from clio_agent.arc.working_set_fold import STEP_OPEN_KIND, FoldingSegmentStore

from .conftest import live_plane_context

SCOPE = "agentA"


def _raw_lane_atoms(arc: ARCMemory, session: str) -> list:
    """Every raw atom on the fold's content lane (bypassing the fold's render)."""
    store = arc._segments
    assert isinstance(store, FoldingSegmentStore)
    atoms: list = []
    for pscope in store._lane_scopes(session):
        atoms.extend(store.list_segments(session, pscope, include_tombstoned=True))
    return atoms


def test_step_open_excluded_from_render_but_on_the_log() -> None:
    """A step_open breadcrumb is on the raw lane yet invisible to every render."""
    session = "so_" + uuid.uuid4().hex[:12]
    arc = ARCMemory(store=_MemoryStore(), working_set_fold=True)
    arc.append_segment(session, SCOPE, "thought", {"text": "T"}, step=0)
    arc._segments.append_step_open(session, SCOPE, {"thought": "T", "tools": ["x"]}, step=0)
    # The breadcrumb is on the raw lane...
    raw_kinds = [a.kind for a in _raw_lane_atoms(arc, session)]
    assert STEP_OPEN_KIND in raw_kinds
    # ...but NOT in any render (working set, full plane, or trajectory keys).
    assert all(s.kind != STEP_OPEN_KIND for s in arc.render_segments(session, SCOPE))
    assert all(s.kind != STEP_OPEN_KIND for s in arc.render_working_set(session, SCOPE))
    assert arc.render_segments_keys(session, SCOPE) == {"thought_0": "T"}


def test_crash_leaves_step_open(monkeypatch: pytest.MonkeyPatch) -> None:
    """A hard crash mid-step (tool execution raising uncaught) still leaves the step's
    ``step_open`` breadcrumb on the canonical log, even though the V2 loop's
    post-execution working-set atoms never land — the fold's crash-recovery guarantee."""
    session = "so_" + uuid.uuid4().hex[:12]
    arc = ARCMemory(store=_MemoryStore(), working_set_fold=True)

    react_cls = runtime._retaining_react_cls()
    agent = react_cls("question -> answer", tools=[dspy.Tool(lambda: "ok", name="probe")])
    lm = DummyLM(
        [{"next_thought": "call probe", "tool_calls": {"tool_calls": [{"name": "probe", "args": {}}]}}]
    )

    # A HARD mid-step failure: tool execution raises uncaught (past the step_open write,
    # before the post-execution _emit_turn). dspy wraps *tool* errors into observations,
    # so we fail the execution stage itself to model an un-recovered crash.
    def _boom(_tool_calls):
        raise RuntimeError("execution stage exploded mid-step")

    monkeypatch.setattr(agent, "_execute_tool_calls", _boom)

    with live_plane_context(arc, session=session, scope=SCOPE):
        with dspy.context(lm=lm, adapter=dspy.ChatAdapter()):
            with pytest.raises(RuntimeError, match="exploded mid-step"):
                agent(question="find alpha")

    raw = _raw_lane_atoms(arc, session)
    step_opens = [a for a in raw if a.kind == STEP_OPEN_KIND]
    assert step_opens, "the crash left no step_open breadcrumb on the log"
    assert step_opens[0].content.get("tools") == ["probe"]
    # The post-execution working-set atoms never landed (the crash preceded them).
    assert arc.render_segments_keys(session, SCOPE) == {}
