"""Acceptance: ARC is provably the source of the prompt (byte-equality) and
out-of-band edits to ARC change the next prompt (mutation-propagation).

These are the decisive contract tests (GOAL.md "Definition of done" #1). They run
the REAL ``_RetainingReAct`` and observe the prompt via ``_format_trajectory`` (the
sole function that renders the trajectory onto the wire) and via a ``PromptRecorder``
at the LM boundary.
"""

from __future__ import annotations

import dspy
from dspy.utils.dummies import DummyLM

from clio_agent.arc.prompt_recorder import PromptRecorder

from .conftest import (
    expected_trajectory_dict,
    live_plane_context,
    make_react_agent,
    stock_format_trajectory,
)

SESSION, SCOPE = "s1", "agentA"


def _run_scripted_loop(agent, arc, recorder=None):
    """Drive a 2-iteration react loop (search, then finish) with a scripted LM."""
    lm = DummyLM(
        [
            {"next_thought": "search first", "next_tool_name": "search",
             "next_tool_args": '{"q": "alpha"}'},
            {"next_thought": "done", "next_tool_name": "finish", "next_tool_args": "{}"},
            {"reasoning": "because", "answer": "FINAL_ANSWER"},
        ]
    )
    cbs = [recorder] if recorder is not None else []
    with live_plane_context(arc, session=SESSION, scope=SCOPE):
        with dspy.context(lm=lm, adapter=dspy.ChatAdapter(), callbacks=cbs):
            return agent(question="find alpha")


def _populate(arc, *triples):
    """Append (kind, content) segments directly (simulating the loop's writes)."""
    for step, (kind, content) in enumerate(triples):
        arc.append_segment(SESSION, SCOPE, kind, content, step=step // 3)


# ---- byte-equality ----------------------------------------------------------


def test_unedited_render_keys_matches_stock_dspy_dict(arc):
    agent = make_react_agent()
    _run_scripted_loop(agent, arc)
    keys = arc.render_segments_keys(SESSION, SCOPE)
    expected = expected_trajectory_dict(
        [
            {"thought": "search first", "tool_name": "search",
             "tool_args": {"q": "alpha"}, "observation": "SEARCH_RESULT"},
            {"thought": "done", "tool_name": "finish",
             "tool_args": {}, "observation": "Completed."},
        ]
    )
    # keys + values + ORDER reproduce exactly what stock dspy's local trajectory holds
    assert list(keys.keys()) == list(expected.keys())
    assert keys["thought_0"] == "search first"
    assert keys["tool_name_0"] == "search"
    assert keys["tool_args_0"] == {"q": "alpha"}
    assert keys["observation_0"] == "SEARCH_RESULT"


def test_format_trajectory_byte_equal_to_stock_formatter(arc):
    agent = make_react_agent()
    _run_scripted_loop(agent, arc)
    with live_plane_context(arc, session=SESSION, scope=SCOPE):
        with dspy.context(adapter=dspy.ChatAdapter()):
            keys = arc.render_segments_keys(SESSION, SCOPE)
            override = agent._format_trajectory({})          # reads ARC
            stock = stock_format_trajectory(agent, keys)     # stock formatter, same keys
    assert override == stock
    assert "SEARCH_RESULT" in override  # and it carries the real content


def test_loop_reads_prompt_from_arc_on_the_wire(arc):
    """The iteration-1 react call must carry iteration-0's observation, which only
    reached the prompt via ARC's render."""
    agent = make_react_agent()
    rec = PromptRecorder()
    _run_scripted_loop(agent, arc, recorder=rec)
    assert any("SEARCH_RESULT" in c.text() for c in rec.calls())


# ---- mutation propagation (decisive) ----------------------------------------
#
# _format_trajectory IS the function that renders the trajectory onto the wire, so
# asserting its output reflects an out-of-band ARC edit is the propagation proof.


def _rendered_after_edit(agent, arc, edit):
    with live_plane_context(arc, session=SESSION, scope=SCOPE):
        with dspy.context(adapter=dspy.ChatAdapter()):
            before = agent._format_trajectory({})
            edit()
            after = agent._format_trajectory({})
    return before, after


def test_append_propagates(arc):
    agent = make_react_agent()
    _populate(arc, ("thought", {"text": "T0"}), ("tool_call", {"name": "a", "args": {}}),
              ("observation", {"text": "O0"}))
    before, after = _rendered_after_edit(
        agent, arc,
        lambda: arc.append_segment(SESSION, SCOPE, "thought", {"text": "APPENDED_X"}, step=1),
    )
    assert "APPENDED_X" not in before
    assert "APPENDED_X" in after


def test_delete_propagates_absent(arc):
    """THE killer test: a deleted segment must vanish from the next prompt — a
    shadow-of-the-local-dict implementation would still show it."""
    agent = make_react_agent()
    _populate(arc, ("thought", {"text": "KEEP_T"}), ("tool_call", {"name": "a", "args": {}}),
              ("observation", {"text": "DELETE_ME"}))
    obs = [s for s in arc.render_segments(SESSION, SCOPE) if s.kind == "observation"][0]
    before, after = _rendered_after_edit(
        agent, arc, lambda: arc.delete_segments(SESSION, SCOPE, [obs.id])
    )
    assert "DELETE_ME" in before
    assert "DELETE_ME" not in after
    assert "KEEP_T" in after  # the rest survives


def test_summarize_propagates(arc):
    agent = make_react_agent()
    _populate(arc, ("thought", {"text": "ORIGINAL_THOUGHT"}),
              ("tool_call", {"name": "a", "args": {}}),
              ("observation", {"text": "ORIGINAL_OBS"}))
    ids = [s.id for s in arc.render_segments(SESSION, SCOPE)]
    before, after = _rendered_after_edit(
        agent, arc,
        lambda: arc.summarize_segments(SESSION, SCOPE, ids, {"text": "SUMMARY_REPLACES_ALL"}),
    )
    assert "ORIGINAL_THOUGHT" in before and "ORIGINAL_OBS" in before
    assert "SUMMARY_REPLACES_ALL" in after
    assert "ORIGINAL_THOUGHT" not in after and "ORIGINAL_OBS" not in after


def test_insert_propagates_at_position(arc):
    agent = make_react_agent()
    _populate(arc, ("thought", {"text": "FIRST"}), ("observation", {"text": "THIRD"}))
    before, after = _rendered_after_edit(
        agent, arc,
        lambda: arc.insert_segment(SESSION, SCOPE, 1, "thought", {"text": "INSERTED_SECOND"}),
    )
    assert "INSERTED_SECOND" not in before
    assert "INSERTED_SECOND" in after


# ---- prefix property (ties to KV reuse) -------------------------------------


def test_append_only_is_a_prefix(arc):
    """An append-only step keeps the prior render as a literal prefix (the
    precondition prefix-caching/KV-reuse depends on)."""
    agent = make_react_agent()
    _populate(arc, ("thought", {"text": "A0"}), ("tool_call", {"name": "t", "args": {}}),
              ("observation", {"text": "B0"}))
    with live_plane_context(arc, session=SESSION, scope=SCOPE):
        with dspy.context(adapter=dspy.ChatAdapter()):
            first = agent._format_trajectory({})
            arc.append_segment(SESSION, SCOPE, "thought", {"text": "A1"}, step=1)
            second = agent._format_trajectory({})
    assert second.startswith(first)  # append did not break the prefix


def test_local_trajectory_dict_is_not_the_source(arc):
    """Even if the local dict says one thing, the prompt follows ARC."""
    agent = make_react_agent()
    _populate(arc, ("thought", {"text": "ARC_TRUTH"}))
    with live_plane_context(arc, session=SESSION, scope=SCOPE):
        with dspy.context(adapter=dspy.ChatAdapter()):
            # a bogus local dict must be ignored; ARC wins
            rendered = agent._format_trajectory({"thought_0": "LOCAL_LIE"})
    assert "ARC_TRUTH" in rendered
    assert "LOCAL_LIE" not in rendered
