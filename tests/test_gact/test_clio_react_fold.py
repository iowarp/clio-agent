"""Pins for the ARC fold seam of ``ClioReAct`` (``clio_react_record``).

These exercise the projection + read seam that fold the MATERIALIZED ARC live plane
into the typed ``dspy.lm15`` messages ``ClioReAct`` sends:

* :func:`fold_steps` -- the pure fold: empty plane, one assistant + one tool message
  per STEP (a step's tool calls + their results together, call ids preserved, results
  matched by call id), a lone summary as a user ``[earlier context]`` message,
  thinking restored with its continuation state, and the typed ``ContextFoldError``
  for an id-less call, an unanswered call, an orphan observation and malformed args.
* :func:`read_steps` -- the read seam: it reads the MATERIALIZED render
  (``render_segments``), NEVER re-derives from the canonical ``_events`` log; it is
  append-only (a new segment extends the prefix); an ARC op (summarize/delete) is the
  sole prefix-reset author; and a plane read failure is a typed
  :class:`ContextReadError` -- there is no fallback to the loop's own step list.

The decisive materialized-read pin: the fold reflects segments in the expert scope
and is EMPTY when only the ``_events`` semantic-event log is populated -- so
re-deriving from the log at read time turns it red.
"""

from __future__ import annotations

import types
from typing import Any

import dspy
import pytest
from dspy.lm15 import ContinuationState, ThinkingPart

import clio_agent.gact.runtime.globals as runtime_globals
from clio_agent.arc.memory import ARCMemory
from clio_agent.arc.schema import Segment
from clio_agent.gact import context as ctx
from clio_agent.gact.agents.clio_react import ClioReAct, NoContextStoreError
from clio_agent.gact.agents.clio_react_record import (
    ContextFoldError,
    ContextReadError,
    arc_scope,
    fold_steps,
    read_steps,
    thinking_from_record,
    thinking_to_record,
)
from tests._scripted_engine import Reply, scripted_lm, summarize

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


def _fold_fails(segs: list[Segment], problem: str) -> ContextFoldError:
    """Assert the fold raises the typed ``ContextFoldError`` for ``problem``."""
    with pytest.raises(ContextFoldError) as err:
        fold_steps(segs)
    assert err.value.error_type == "context_fold_failed"
    assert err.value.details["problem"] == problem
    return err.value


# --- 1. the pure fold ----------------------------------------------------------


def test_empty_plane_folds_to_no_messages() -> None:
    assert fold_steps([]) == []


def test_single_full_step_folds_to_an_assistant_and_a_tool_message() -> None:
    segs = [
        _seg("thought", {"text": "T0"}, order=1),
        _call("call_0_0", "search", {"q": "alpha"}, order=2),
        _obs("call_0_0", "OBS0", order=3),
    ]
    assert summarize(fold_steps(segs)) == [
        ("assistant", [("text", "T0"), ("call", "call_0_0", "search", {"q": "alpha"})]),
        ("tool", [("result", "call_0_0", "OBS0", False)]),
    ]


def test_step_with_two_calls_folds_into_one_step_with_both_results() -> None:
    """A step's concurrent calls render as the ONE step they were: both calls in one
    assistant message and both results in one tool message, results matched to their
    calls by id (not by order), error flags preserved."""
    segs = [
        _seg("thought", {"text": "both"}, order=1),
        _call("call_0_0", "search", {"q": "a"}, order=2),
        _call("call_0_1", "fetch", {"url": "u"}, order=3),
        # results land out of call order: matching is by call id
        _obs("call_0_1", "FETCH_FAILED", order=4, is_error=True),
        _obs("call_0_0", "SEARCH_OK", order=5),
    ]
    assert summarize(fold_steps(segs)) == [
        (
            "assistant",
            [
                ("text", "both"),
                ("call", "call_0_0", "search", {"q": "a"}),
                ("call", "call_0_1", "fetch", {"url": "u"}),
            ],
        ),
        (
            "tool",
            [
                ("result", "call_0_0", "SEARCH_OK", False),
                ("result", "call_0_1", "FETCH_FAILED", True),
            ],
        ),
    ]


def test_idless_call_fails_typed() -> None:
    """A call segment written without an id cannot be matched to its result: the fold
    raises instead of inventing a positional id."""
    segs = [
        _seg("thought", {"text": "T"}, order=1),
        _seg("tool_call", {"name": "a", "args": {}}, order=2),
    ]
    _fold_fails(segs, "malformed_call")


def test_call_unanswered_at_the_end_of_the_plane_fails_typed() -> None:
    """A call whose observation is gone (only the observation was deleted) leaves the
    step incoherent: the fold raises rather than sending a call with no result."""
    segs = [
        _seg("thought", {"text": "T"}, order=1),
        _call("call_0_0", "a", {}, order=2),
        _call("call_0_1", "b", {}, order=3),
        _obs("call_0_1", "OB", order=4),
    ]
    err = _fold_fails(segs, "unanswered_call")
    assert "call_0_0" in str(err)


def test_multi_step_grouping_and_ordering() -> None:
    """Two full steps fold to two ordered assistant/tool pairs, call ids preserved."""
    segs = [
        _seg("thought", {"text": "T0"}, order=1),
        _call("call_0_0", "a", {}, order=2),
        _obs("call_0_0", "O0", order=3),
        _seg("thought", {"text": "T1"}, order=4),
        _call("call_1_0", "b", {"x": 1}, order=5),
        _obs("call_1_0", "O1", order=6),
    ]
    assert summarize(fold_steps(segs)) == [
        ("assistant", [("text", "T0"), ("call", "call_0_0", "a", {})]),
        ("tool", [("result", "call_0_0", "O0", False)]),
        ("assistant", [("text", "T1"), ("call", "call_1_0", "b", {"x": 1})]),
        ("tool", [("result", "call_1_0", "O1", False)]),
    ]


def test_thinking_folds_back_byte_exact_with_its_continuation() -> None:
    """A step's thinking is stored with the provider continuation state and folds back
    as the same ``ThinkingPart`` (the provider needs it back unchanged)."""
    part = ThinkingPart(
        text="plan",
        continuation=(
            ContinuationState(provider="openai", kind="encrypted", data={"blob": "x=="}),
        ),
    )
    assert thinking_from_record(thinking_to_record(part)) == part
    segs = [
        _seg("thought", {"text": "T", "thinking": [thinking_to_record(part)]}, order=1),
        _call("call_0_0", "a", {}, order=2),
        _obs("call_0_0", "O", order=3),
    ]
    assert fold_steps(segs)[0].parts[0] == part


def test_lone_summary_segment_surfaces_as_earlier_context() -> None:
    """A compaction ``summary`` still reaches the wire, as a user message."""
    assert summarize(fold_steps([_seg("summary", {"text": "COMPACTED"}, order=1)])) == [
        ("user", [("text", "[earlier context]\nCOMPACTED")])
    ]


def test_observation_after_its_step_is_answered_fails_typed() -> None:
    """An observation with no call awaiting a result (an id-less note written after a
    fully answered step) is not turned into a user message: the fold raises."""
    segs = [
        _seg("thought", {"text": "T0"}, order=1),
        _call("call_0_0", "a", {}, order=2),
        _obs("call_0_0", "O0", order=3),
        _seg("observation", {"text": "ORPHAN"}, order=4),
    ]
    _fold_fails(segs, "orphan_observation")


def test_a_thought_without_text_folds_but_non_mapping_args_fail_typed() -> None:
    """A thought with no text is a valid (empty) step and non-context kinds are skipped,
    but a call whose args are not a mapping is malformed: the fold raises instead of
    sending the call with ``{}`` args."""
    assert summarize(
        fold_steps([_seg("thought", {}, order=1), _seg("answer", {"text": "IGNORED"}, order=2)])
    ) == [("assistant", [("text", "")])]
    segs = [
        _seg("thought", {}, order=1),
        _seg("tool_call", {"id": "call_0_0", "name": "t", "args": "notadict"}, order=2),
    ]
    _fold_fails(segs, "malformed_call")


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
    folded = summarize(read_steps(arc, SESSION, SCOPE))
    assert folded[1] == ("tool", [("result", "call_0_0", "MATERIALIZED_OBS", False)])


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
    """No active react scope -> the plane is not the source (the loop's own steps are)."""
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
    """An append extends the folded message prefix (the cache precondition)."""
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
    assert summarize(before)[0][1][0] == ("text", "ORIGINAL")
    arc.summarize_segments(SESSION, SCOPE, ids, {"text": "SUMMARY_REPLACES_ALL"})
    after = read_steps(arc, SESSION, SCOPE)
    assert summarize(after) == [("user", [("text", "[earlier context]\nSUMMARY_REPLACES_ALL")])]
    assert after[: len(before)] != before, "summarize must reset, not extend"


def test_read_seam_failure_is_typed() -> None:
    """A plane read failure is a typed ``ContextReadError`` carrying the scope and the
    cause -- never swallowed, never an empty context."""

    class _BrokenArc:
        def context_view(self, session: str, scope: str) -> Any:
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
    closes the lifecycle ``failed`` -- it does NOT fall back to its own steps, so the
    model is never called on a context the plane did not produce."""

    def _broken(session: str, scope: str) -> Any:
        raise OSError("plane unreadable")

    monkeypatch.setattr(arc, "context_view", _broken)
    lifecycle: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        runtime_globals,
        "_emit_expert_lifecycle_event",
        lambda event_type, **kw: lifecycle.append((event_type, kw)),
    )

    lm, engine = scripted_lm([Reply(text="never")])
    agent = ClioReAct("question -> answer", tools=[dspy.Tool(lambda q: "R", name="search")])

    app_token = ctx.set_app(types.SimpleNamespace(state=types.SimpleNamespace(arc=arc)))
    scope_token = ctx.set_react_scope(SCOPE)
    session_token = ctx.set_react_session(SESSION)
    try:
        with dspy.context(lm=lm), pytest.raises(ContextReadError):
            agent(question="q")
    finally:
        ctx.reset(session_token)
        ctx.reset(scope_token)
        ctx.reset(app_token)

    assert engine.requests == [], "the model must not be called on a fallback context"
    assert [t for t, _ in lifecycle] == ["expert.lifecycle.started", "expert.lifecycle.failed"]
    assert lifecycle[-1][1]["payload"]["reason"] == "arc_context_read_failed"


def test_loop_without_a_plane_fails_typed() -> None:
    """No clio-core scope bound: the loop has no context store and fails typed before
    any model call -- it never runs on a private step list."""
    lm, engine = scripted_lm([Reply(text="done")])
    agent = ClioReAct("question -> answer", tools=[dspy.Tool(lambda q: "OWN_OBS", name="search")])
    with dspy.context(lm=lm), pytest.raises(NoContextStoreError) as err:
        agent(question="q")
    assert err.value.error_type == "no_context_store"
    assert engine.requests == []
