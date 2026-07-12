"""S2 pins for the ARC fold seam of the clio ``ReActV2`` subclass (#901 S2).

These exercise the projection + read seam that fold the MATERIALIZED ARC live plane
into the ``dspy.History`` message list ReActV2 consumes:

* :func:`segments_to_messages` — the pure fold (V2 sibling of ``segments_to_keys``):
  empty plane, multi-segment turn grouping/ordering, tool-result merge, a lone
  summary/observation, and malformed ("wrong-input") segment content.
* :func:`arc_history_messages` / :func:`override_history_inputs_from_arc` — the read
  seam: it reads the MATERIALIZED render (``render_segments``), NEVER re-derives from
  the canonical ``_events`` log; it is append-only (a new segment extends the prefix);
  and an ARC op (summarize/delete) is the sole prefix-reset author (propagation).

The decisive materialized-read pin (sabotage target a): the fold reflects segments in
the expert scope and is EMPTY when only the ``_events`` semantic-event log is
populated — so re-deriving from the log at read time turns it red.
"""

from __future__ import annotations

import types
from typing import Any, Iterator

import dspy
import pytest
from dspy.adapters.types.tool import ToolCalls

from clio_agent.arc.memory import ARCMemory
from clio_agent.arc.schema import Segment
from clio_agent.gact import context as ctx
from clio_agent.gact.agents.reactv2 import (
    arc_history_messages,
    override_history_inputs_from_arc,
    segments_to_messages,
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


def _scope_ctx(arc_memory: ARCMemory) -> Iterator[None]:
    app = types.SimpleNamespace(state=types.SimpleNamespace(arc=arc_memory))
    t1 = ctx.set_app(app)
    t2 = ctx.set_react_scope(SCOPE)
    t3 = ctx.set_react_session(SESSION)
    try:
        yield
    finally:
        ctx.reset(t3)
        ctx.reset(t2)
        ctx.reset(t1)


# --- 1. the pure fold ----------------------------------------------------------


def test_empty_plane_folds_to_no_messages() -> None:
    assert segments_to_messages([]) == []


def test_single_full_turn_folds_to_one_event_with_tool_result() -> None:
    segs = [
        _seg("thought", {"text": "T0"}, order=1),
        _seg("tool_call", {"name": "search", "args": {"q": "alpha"}}, order=2),
        _seg("observation", {"text": "OBS0"}, order=3),
    ]
    messages = segments_to_messages(segs)
    assert len(messages) == 1
    event = messages[0]
    assert event["next_thought"] == "T0"
    tc = event["tool_calls"]
    assert isinstance(tc, ToolCalls)
    assert [c.name for c in tc.tool_calls] == ["search"]
    assert tc.tool_calls[0].args == {"q": "alpha"}
    assert tc.tool_calls[0].id == "call_0_0"
    # observation merged in as the tool call's result (id lines up with the call).
    results = tc.tool_call_results.tool_call_results
    assert [r.value for r in results] == ["OBS0"]
    assert results[0].call_id == "call_0_0"
    assert results[0].is_error is False


def test_multi_segment_turn_grouping_and_ordering() -> None:
    """Two full turns fold to two ordered events, gapless like ``segments_to_keys``."""
    segs = [
        _seg("thought", {"text": "T0"}, order=1),
        _seg("tool_call", {"name": "a", "args": {}}, order=2),
        _seg("observation", {"text": "O0"}, order=3),
        _seg("thought", {"text": "T1"}, order=4),
        _seg("tool_call", {"name": "b", "args": {"x": 1}}, order=5),
        _seg("observation", {"text": "O1"}, order=6),
    ]
    messages = segments_to_messages(segs)
    assert [m["next_thought"] for m in messages] == ["T0", "T1"]
    assert [m["tool_calls"].tool_calls[0].name for m in messages] == ["a", "b"]
    assert [m["tool_calls"].tool_calls[0].id for m in messages] == ["call_0_0", "call_1_0"]


def test_lone_summary_segment_surfaces_as_text() -> None:
    """A compaction ``summary`` (rendered as an observation with no owning tool call)
    still reaches the wire as a text event (its content is not dropped)."""
    messages = segments_to_messages([_seg("summary", {"text": "COMPACTED"}, order=1)])
    assert messages == [{"next_thought": "COMPACTED"}]


def test_wrong_input_content_does_not_raise() -> None:
    """Malformed segment content (missing text, non-dict args, unknown kind) folds
    without raising — a bad write can never break the read seam."""
    segs = [
        _seg("thought", {}, order=1),  # missing "text"
        _seg("tool_call", {"name": "t", "args": "notadict"}, order=2),  # bad args
        _seg("answer", {"text": "IGNORED"}, order=3),  # non-working-set kind
    ]
    messages = segments_to_messages(segs)
    # One turn (thought+tool_call); the non-working-set 'answer' kind is ignored by
    # the underlying segments_to_keys allowlist.
    assert len(messages) == 1
    assert messages[0]["next_thought"] == ""
    assert messages[0]["tool_calls"].tool_calls[0].args == {}


# --- 2. the read seam: materialized plane, never the log -----------------------


def test_read_seam_folds_the_materialized_expert_scope(arc) -> None:
    """arc_history_messages reads the MATERIALIZED render of the expert scope."""
    arc.append_segment(SESSION, SCOPE, "thought", {"text": "T0"}, step=0)
    arc.append_segment(SESSION, SCOPE, "tool_call", {"name": "search", "args": {}}, step=0)
    arc.append_segment(SESSION, SCOPE, "observation", {"text": "MATERIALIZED_OBS"}, step=0)
    gen = _scope_ctx(arc)
    next(gen)
    try:
        messages = arc_history_messages()
        assert messages is not None
        results = messages[0]["tool_calls"].tool_call_results.tool_call_results
        assert results[0].value == "MATERIALIZED_OBS"
    finally:
        next(gen, None)


def test_read_seam_is_empty_when_only_the_event_log_is_populated(arc) -> None:
    """SABOTAGE PIN (materialized-read): folding the expert scope reads the live
    working set, NOT the canonical ``_events`` semantic-event log. With ONLY the
    event log populated (no expert-scope working set), the fold is empty (-> None).
    A read seam that re-derived from the log would return non-empty here."""

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
    gen = _scope_ctx(arc)
    next(gen)
    try:
        assert arc_history_messages() is None
    finally:
        next(gen, None)


def test_read_seam_none_without_scope(arc) -> None:
    """No active react scope -> ARC is not the source (use V2's own history)."""
    assert arc_history_messages() is None


def test_read_seam_append_only_prefix(arc) -> None:
    """An append extends the folded message prefix (the #891 cache precondition)."""
    arc.append_segment(SESSION, SCOPE, "thought", {"text": "T0"}, step=0)
    arc.append_segment(SESSION, SCOPE, "tool_call", {"name": "a", "args": {}}, step=0)
    arc.append_segment(SESSION, SCOPE, "observation", {"text": "O0"}, step=0)
    gen = _scope_ctx(arc)
    next(gen)
    try:
        first = arc_history_messages()
        arc.append_segment(SESSION, SCOPE, "thought", {"text": "T1"}, step=1)
        second = arc_history_messages()
        assert first is not None and second is not None
        assert second[: len(first)] == first, "append did not preserve the prefix"
        assert len(second) > len(first)
    finally:
        next(gen, None)


def test_read_seam_summarize_op_resets_the_prefix(arc) -> None:
    """SABOTAGE PIN (ops-are-the-sole-reset-author): a summarize op replaces the live
    working set, so the fold RESETS (the pre-op messages are no longer a prefix and
    the original content is gone) — a mutation an append-only-only seam cannot show."""
    arc.append_segment(SESSION, SCOPE, "thought", {"text": "ORIGINAL"}, step=0)
    arc.append_segment(SESSION, SCOPE, "tool_call", {"name": "a", "args": {}}, step=0)
    arc.append_segment(SESSION, SCOPE, "observation", {"text": "ORIGINAL_OBS"}, step=0)
    ids = [s.id for s in arc.render_segments(SESSION, SCOPE)]
    gen = _scope_ctx(arc)
    next(gen)
    try:
        before = arc_history_messages()
        assert before is not None and before[0]["next_thought"] == "ORIGINAL"
        arc.summarize_segments(SESSION, SCOPE, ids, {"text": "SUMMARY_REPLACES_ALL"})
        after = arc_history_messages()
        assert after is not None
        assert after == [{"next_thought": "SUMMARY_REPLACES_ALL"}]
        assert after[: len(before)] != before, "summarize must reset, not extend"
    finally:
        next(gen, None)


# --- 3. end-to-end wire render through the adapter override --------------------


def _react_signature() -> Any:
    from clio_agent.gact.agents.reactv2 import _RetainingReActV2

    agent = _RetainingReActV2("question -> answer", tools=[dspy.Tool(lambda q: "R", name="search")])
    return agent.react.signature, list(agent.tools.values())


def test_adapter_override_sources_wire_from_arc_and_propagates(arc) -> None:
    """The LenientChatAdapter override renders the wire from ARC even though the
    passed-in History is empty, and an out-of-band delete propagates to the next
    render — ARC is provably the wire source."""
    arc.append_segment(SESSION, SCOPE, "thought", {"text": "WIRE_T0"}, step=0)
    arc.append_segment(SESSION, SCOPE, "tool_call", {"name": "search", "args": {}}, step=0)
    arc.append_segment(SESSION, SCOPE, "observation", {"text": "WIRE_OBS"}, step=0)

    signature, tools = _react_signature()
    adapter = _lenient_chat_adapter_cls()()

    def _wire_text() -> str:
        inputs = {
            "question": "q",
            "history": dspy.History(messages=[]),  # deliberately empty; ARC must win
            "tools": tools,
        }
        with dspy.context(adapter=adapter):
            messages = adapter.format(signature, [], inputs)
        return "\n".join(str(m.get("content") or "") for m in messages)

    gen = _scope_ctx(arc)
    next(gen)
    try:
        before = _wire_text()
        assert "WIRE_OBS" in before, "ARC content did not reach the wire"
        obs = [s for s in arc.render_segments(SESSION, SCOPE) if s.kind == "observation"][0]
        arc.delete_segments(SESSION, SCOPE, [obs.id])
        after = _wire_text()
        assert "WIRE_OBS" not in after, "a deleted segment must vanish from the next prompt"
        assert "WIRE_T0" in after, "the rest of the working set survives"
    finally:
        next(gen, None)


def test_override_returns_false_and_leaves_history_when_arc_absent() -> None:
    """No ARC scope: the override no-ops and the passed History is unchanged (a
    standalone V2 loop renders its own internal append-only history)."""
    passed = dspy.History(messages=[{"next_thought": "OWN"}])
    inputs = {"history": passed}
    assert override_history_inputs_from_arc(inputs, "history") is False
    assert inputs["history"] is passed
