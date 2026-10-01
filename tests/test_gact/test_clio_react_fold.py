"""Pins for the ARC fold seam of ``ClioReAct`` (``clio_react_record``).

These exercise the projection + read seam that fold the MATERIALIZED ARC live plane
into the ``dspy.History`` events ``ClioReAct`` sends:

* :func:`fold_steps` -- the pure fold: empty plane, one event per STEP (a step's tool
  calls + their results together, call ids preserved, results matched by call id),
  a lone summary / orphan observation as its own ``next_thought`` event, and
  malformed ("wrong-input") segment content.
* :func:`read_steps` -- the read seam: it reads the MATERIALIZED render
  (``render_segments``), NEVER re-derives from the canonical ``_events`` log; it is
  append-only (a new segment extends the prefix); an ARC op (summarize/delete) is the
  sole prefix-reset author; and a plane read failure is a typed
  :class:`ContextReadError` -- there is no fallback to the loop's own History.

The decisive materialized-read pin: the fold reflects segments in the expert scope
and is EMPTY when only the ``_events`` semantic-event log is populated -- so
re-deriving from the log at read time turns it red.
"""

from __future__ import annotations

import types
from typing import Any

import dspy
import pytest
from dspy.adapters.types.tool import ToolCalls
from dspy.utils.dummies import DummyLM

import clio_agent.gact.runtime.globals as runtime_globals
from clio_agent.arc.memory import ARCMemory
from clio_agent.arc.schema import Segment
from clio_agent.gact import context as ctx
from clio_agent.gact.agents.clio_react import ClioReAct
from clio_agent.gact.agents.clio_react_record import (
    ContextReadError,
    arc_scope,
    fold_steps,
    read_steps,
)
from clio_agent.lm.adapters import _lenient_chat_adapter_cls

SESSION, SCOPE = "s1", "agentA"


@pytest.fixture
def arc(tmp_path) -> ARCMemory:
    return ARCMemory(data_dir=str(tmp_path / "arc"))


def _seg(kind: str, content: dict[str, Any], *, order: float) -> Segment:
    """A minimal live Segment for the pure-fold tests (no store needed)."""
    return Segment(
        scope=SCOPE,
        kind=kind,  # type: ignore[arg-type]
        content=content,
        session_id=SESSION,
        step=0,
        order=order,
        logical_time=int(order),
    )


def _call(call_id: str, name: str, args: dict[str, Any], *, order: float) -> Segment:
    return _seg("tool_call", {"id": call_id, "name": name, "args": args}, order=order)


def _obs(call_id: str, text: str, *, order: float, is_error: bool = False) -> Segment:
    return _seg(
        "observation", {"call_id": call_id, "text": text, "is_error": is_error}, order=order
    )


# --- 1. the pure fold ----------------------------------------------------------


def test_empty_plane_folds_to_no_events() -> None:
    assert fold_steps([]) == []


def test_single_full_step_folds_to_one_event_with_tool_result() -> None:
    segs = [
        _seg("thought", {"text": "T0"}, order=1),
        _call("call_0_0", "search", {"q": "alpha"}, order=2),
        _obs("call_0_0", "OBS0", order=3),
    ]
    events = fold_steps(segs)
    assert len(events) == 1
    event = events[0]
    assert event["next_thought"] == "T0"
    tc = event["tool_calls"]
    assert isinstance(tc, ToolCalls)
    assert [c.name for c in tc.tool_calls] == ["search"]
    assert tc.tool_calls[0].args == {"q": "alpha"}
    assert tc.tool_calls[0].id == "call_0_0"
    # the observation is merged in as the call's result (id lines up with the call).
    results = tc.tool_call_results.tool_call_results
    assert [r.value for r in results] == ["OBS0"]
    assert results[0].call_id == "call_0_0"
    assert results[0].is_error is False


def test_step_with_two_calls_folds_into_one_event_with_both_results() -> None:
    """A step's concurrent calls render as the ONE step they were: both calls and both
    results in a single event, results matched to their calls by id (not by order),
    error flags preserved."""
    segs = [
        _seg("thought", {"text": "both"}, order=1),
        _call("call_0_0", "search", {"q": "a"}, order=2),
        _call("call_0_1", "fetch", {"url": "u"}, order=3),
        # results land out of call order: matching is by call id
        _obs("call_0_1", "FETCH_FAILED", order=4, is_error=True),
        _obs("call_0_0", "SEARCH_OK", order=5),
    ]
    events = fold_steps(segs)
    assert len(events) == 1
    tc = events[0]["tool_calls"]
    assert [(c.id, c.name, c.args) for c in tc.tool_calls] == [
        ("call_0_0", "search", {"q": "a"}),
        ("call_0_1", "fetch", {"url": "u"}),
    ]
    results = tc.tool_call_results.tool_call_results
    assert [(r.call_id, r.value, r.is_error) for r in results] == [
        ("call_0_0", "SEARCH_OK", False),
        ("call_0_1", "FETCH_FAILED", True),
    ]


def test_idless_segments_match_results_by_order() -> None:
    """A segment written without a call id (an older writer) still folds: the calls get
    positional ids and observations attach in call order."""
    segs = [
        _seg("thought", {"text": "T"}, order=1),
        _seg("tool_call", {"name": "a", "args": {}}, order=2),
        _seg("tool_call", {"name": "b", "args": {}}, order=3),
        _seg("observation", {"text": "OA"}, order=4),
        _seg("observation", {"text": "OB"}, order=5),
    ]
    (event,) = fold_steps(segs)
    tc = event["tool_calls"]
    assert [c.id for c in tc.tool_calls] == ["call_0", "call_1"]
    results = tc.tool_call_results.tool_call_results
    assert [(r.call_id, r.value) for r in results] == [("call_0", "OA"), ("call_1", "OB")]


def test_unanswered_call_folds_without_a_result() -> None:
    """A call whose observation is gone (deleted / not yet written) keeps its call and
    carries no result for it."""
    segs = [
        _seg("thought", {"text": "T"}, order=1),
        _call("call_0_0", "a", {}, order=2),
        _call("call_0_1", "b", {}, order=3),
        _obs("call_0_1", "OB", order=4),
    ]
    (event,) = fold_steps(segs)
    tc = event["tool_calls"]
    assert [c.id for c in tc.tool_calls] == ["call_0_0", "call_0_1"]
    assert [r.call_id for r in tc.tool_call_results.tool_call_results] == ["call_0_1"]


def test_multi_step_grouping_and_ordering() -> None:
    """Two full steps fold to two ordered events, call ids preserved."""
    segs = [
        _seg("thought", {"text": "T0"}, order=1),
        _call("call_0_0", "a", {}, order=2),
        _obs("call_0_0", "O0", order=3),
        _seg("thought", {"text": "T1"}, order=4),
        _call("call_1_0", "b", {"x": 1}, order=5),
        _obs("call_1_0", "O1", order=6),
    ]
    events = fold_steps(segs)
    assert [e["next_thought"] for e in events] == ["T0", "T1"]
    assert [e["tool_calls"].tool_calls[0].name for e in events] == ["a", "b"]
    assert [e["tool_calls"].tool_calls[0].id for e in events] == ["call_0_0", "call_1_0"]


def test_lone_summary_segment_surfaces_as_text() -> None:
    """A compaction ``summary`` still reaches the wire as its own text event."""
    assert fold_steps([_seg("summary", {"text": "COMPACTED"}, order=1)]) == [
        {"next_thought": "COMPACTED"}
    ]


def test_orphan_observation_closes_the_step_and_is_its_own_event() -> None:
    """An observation with no call awaiting a result (e.g. the ``[turn escalated]``
    note) closes the open step and surfaces as its own ``next_thought`` event."""
    segs = [
        _seg("thought", {"text": "T0"}, order=1),
        _call("call_0_0", "a", {}, order=2),
        _obs("call_0_0", "O0", order=3),
        _seg("observation", {"text": "ORPHAN"}, order=4),
    ]
    events = fold_steps(segs)
    assert [e["next_thought"] for e in events] == ["T0", "ORPHAN"]
    assert "tool_calls" not in events[1]


def test_wrong_input_content_does_not_raise() -> None:
    """Malformed segment content (missing text, non-dict args, unknown kind) folds
    without raising -- a bad write can never break the read seam."""
    segs = [
        _seg("thought", {}, order=1),  # missing "text"
        _seg("tool_call", {"name": "t", "args": "notadict"}, order=2),  # bad args
        _seg("answer", {"text": "IGNORED"}, order=3),  # non-working-set kind
    ]
    events = fold_steps(segs)
    assert len(events) == 1
    assert events[0]["next_thought"] == ""
    assert events[0]["tool_calls"].tool_calls[0].args == {}


# --- 2. the read seam: materialized plane, never the log -----------------------


def _populate_step(arc: ARCMemory, step: int, thought: str, obs: str) -> None:
    call_id = f"call_{step}_0"
    arc.append_segment(SESSION, SCOPE, "thought", {"text": thought}, step=step)
    arc.append_segment(
        SESSION, SCOPE, "tool_call", {"id": call_id, "name": "a", "args": {}}, step=step
    )
    arc.append_segment(SESSION, SCOPE, "observation", {"call_id": call_id, "text": obs}, step=step)


def test_read_seam_folds_the_materialized_expert_scope(arc) -> None:
    _populate_step(arc, 0, "T0", "MATERIALIZED_OBS")
    events = read_steps(arc, SESSION, SCOPE)
    results = events[0]["tool_calls"].tool_call_results.tool_call_results
    assert results[0].value == "MATERIALIZED_OBS"


def test_read_seam_is_empty_when_only_the_event_log_is_populated(arc) -> None:
    """SABOTAGE PIN (materialized-read): folding the expert scope reads the live
    working set, NOT the canonical ``_events`` semantic-event log. With ONLY the event
    log populated the fold is EMPTY; a read seam that re-derived from the log would
    return a non-empty working set here."""

    class _Event:
        session_id = SESSION
        turn_id = "t"
        expert_span_id = ""
        event_type = "expert.response.completed"
        status = "completed"
        summary = ""
        actor: dict = {"agent_id": "agentA"}
        subject: dict = {}
        payload: dict = {"answer": "FROM_LOG"}
        provider: dict = {}
        occurred_at = ""
        trace_id = ""

    arc.record_semantic_event(_Event())  # populates the reserved _events log only
    assert read_steps(arc, SESSION, SCOPE) == []


def test_arc_scope_is_none_without_a_react_scope(arc) -> None:
    """No active react scope -> the plane is not the source (the loop's own History is)."""
    app_token = ctx.set_app(types.SimpleNamespace(state=types.SimpleNamespace(arc=arc)))
    try:
        assert arc_scope() == (None, "", "")
    finally:
        ctx.reset(app_token)


def test_arc_scope_resolves_the_live_plane(arc) -> None:
    app_token = ctx.set_app(types.SimpleNamespace(state=types.SimpleNamespace(arc=arc)))
    scope_token = ctx.set_react_scope(SCOPE)
    session_token = ctx.set_react_session(SESSION)
    try:
        assert arc_scope() == (arc, SESSION, SCOPE)
    finally:
        ctx.reset(session_token)
        ctx.reset(scope_token)
        ctx.reset(app_token)


def test_read_seam_append_only_prefix(arc) -> None:
    """An append extends the folded event prefix (the cache precondition)."""
    _populate_step(arc, 0, "T0", "O0")
    first = read_steps(arc, SESSION, SCOPE)
    arc.append_segment(SESSION, SCOPE, "thought", {"text": "T1"}, step=1)
    second = read_steps(arc, SESSION, SCOPE)
    assert second[: len(first)] == first, "append did not preserve the prefix"
    assert len(second) > len(first)


def test_read_seam_summarize_op_resets_the_prefix(arc) -> None:
    """SABOTAGE PIN (ops-are-the-sole-reset-author): a summarize op replaces the live
    working set, so the fold RESETS (the pre-op events are no longer a prefix and the
    original content is gone)."""
    _populate_step(arc, 0, "ORIGINAL", "ORIGINAL_OBS")
    ids = [s.id for s in arc.render_segments(SESSION, SCOPE)]
    before = read_steps(arc, SESSION, SCOPE)
    assert before[0]["next_thought"] == "ORIGINAL"
    arc.summarize_segments(SESSION, SCOPE, ids, {"text": "SUMMARY_REPLACES_ALL"})
    after = read_steps(arc, SESSION, SCOPE)
    assert after == [{"next_thought": "SUMMARY_REPLACES_ALL"}]
    assert after[: len(before)] != before, "summarize must reset, not extend"


def test_read_seam_failure_is_typed() -> None:
    """A plane read failure is a typed ``ContextReadError`` carrying the scope and the
    cause -- never swallowed, never an empty context."""

    class _BrokenArc:
        def render_segments(self, session: str, scope: str) -> list[Any]:
            raise OSError("plane unreadable")

    with pytest.raises(ContextReadError) as err:
        read_steps(_BrokenArc(), SESSION, SCOPE)
    assert err.value.reason == "arc_context_read_failed"
    assert isinstance(err.value.__cause__, OSError)
    assert SCOPE in str(err.value)


def test_loop_fails_typed_on_a_plane_read_failure_without_fallback(
    arc, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End to end: when the plane read fails the loop raises ``ContextReadError`` and
    closes the lifecycle ``failed`` -- it does NOT fall back to its own History, so the
    model is never called on a context the plane did not produce."""

    def _broken(session: str, scope: str) -> list[Any]:
        raise OSError("plane unreadable")

    monkeypatch.setattr(arc, "render_segments", _broken)
    lifecycle: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        runtime_globals,
        "_emit_expert_lifecycle_event",
        lambda event_type, **kw: lifecycle.append((event_type, kw)),
    )

    adapter = _lenient_chat_adapter_cls()()
    lm = DummyLM([{"next_thought": "never", "tool_calls": '{"tool_calls": []}'}], adapter=adapter)
    agent = ClioReAct("question -> answer", tools=[dspy.Tool(lambda q: "R", name="search")])

    app_token = ctx.set_app(types.SimpleNamespace(state=types.SimpleNamespace(arc=arc)))
    scope_token = ctx.set_react_scope(SCOPE)
    session_token = ctx.set_react_session(SESSION)
    try:
        with dspy.context(lm=lm, adapter=adapter), pytest.raises(ContextReadError):
            agent(question="q")
    finally:
        ctx.reset(session_token)
        ctx.reset(scope_token)
        ctx.reset(app_token)

    assert lm.history == [], "the model must not be called on a fallback context"
    assert [t for t, _ in lifecycle] == ["expert.lifecycle.started", "expert.lifecycle.failed"]
    assert lifecycle[-1][1]["payload"]["reason"] == "arc_context_read_failed"


def test_loop_without_a_plane_uses_its_own_history() -> None:
    """No ARC scope: the loop's context is its own History (the prior step reaches the
    next call) -- the only other context source, chosen up front, not a fallback."""
    adapter = _lenient_chat_adapter_cls()()
    lm = DummyLM(
        [
            {
                "next_thought": "look",
                "tool_calls": '{"tool_calls": [{"name": "search", "args": {"q": "x"}}]}',
            },
            {"next_thought": "done", "tool_calls": '{"tool_calls": []}'},
        ],
        adapter=adapter,
    )
    agent = ClioReAct("question -> answer", tools=[dspy.Tool(lambda q: "OWN_OBS", name="search")])
    with dspy.context(lm=lm, adapter=adapter):
        pred = agent(question="q")
    assert pred.termination_reason == "direct_response"
    assert "OWN_OBS" in str(lm.history[1]["messages"])
