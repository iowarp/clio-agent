"""Acceptance: the ``ClioReAct`` wire is PROVEN byte-for-byte against an independently
built reference, and out-of-band ARC edits change the next prompt.

The reference is :func:`expected_history_messages` (one ``dspy.History`` event per
step, built WITHOUT ``fold_steps``) behind the static head event
``{question, tools}``, formatted by clio's adapter. Two invariants are pinned:

* **the ARC fold of the plane == what the loop sent** -- every model call's messages
  equal the adapter formatting ``[head, *expected_history_messages(steps so far)]``,
  and the loop's own History equals the fold of what it wrote to the plane;
* **append-only** -- consecutive calls are strict prefix extensions beneath one
  byte-identical closing ``Respond with...`` message (the invariant the Claude
  stateful session-delta transport needs).

Formalized shape (NOT silently absorbed):

* (a) the task inputs (``question`` + ``tools``) ride ONCE in the HEAD event, a
  user-only message, and are never re-rendered as a moving trailing block;
  ``test_deviation_a_inputs_folded_into_head`` pins it against stock
  ``dspy.ReActV2`` as the foil;
* (b) a summary / orphan observation (no owning tool call) surfaces as its own
  ``next_thought`` event; ``test_deviation_b_summary_surfaces_as_next_thought``.

Sabotage: reorder or mutate a folded event in ``fold_steps`` → the reference tests go
red (the fold diverges from the fixed, independently-built reference).
"""

from __future__ import annotations

from typing import Any

import dspy
from dspy.adapters.types.tool import ToolCallResults, ToolCalls
from dspy.utils.dummies import DummyLM

from clio_agent.gact.agents.clio_react import ClioReAct
from clio_agent.gact.agents.clio_react_record import fold_steps, read_steps
from clio_agent.lm.adapters import _lenient_chat_adapter_cls

from .conftest import live_plane_context

SESSION, SCOPE = "s1", "agentA"
QUESTION = "find alpha"


# ---- the independent reference -------------------------------------------------


def expected_history_messages(steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The byte-equality reference: the per-step ``dspy.History`` events a fully
    populated plane folds to -- built INDEPENDENTLY of ``fold_steps``.

    Each event carries ``next_thought`` + ``tool_calls`` (with their results, matched by
    call id ``call_<step>_0``) ONLY -- no task input (that rides the head event).
    ``steps`` is a list of ``{thought, tool_name, tool_args, observation}``.
    """
    messages: list[dict[str, Any]] = []
    for i, s in enumerate(steps):
        call = ToolCalls.ToolCall(id=f"call_{i}_0", name=s["tool_name"], args=s["tool_args"])
        tool_calls = ToolCalls(tool_calls=[call])
        tool_calls = tool_calls.model_copy(
            update={
                "tool_call_results": ToolCallResults.from_tool_calls_and_values(
                    tool_calls, [s["observation"]], [False]
                )
            }
        )
        messages.append({"next_thought": s["thought"], "tool_calls": tool_calls})
    return messages


def _search(q: str) -> str:
    """A deterministic search tool."""
    return {"alpha": "SEARCH_RESULT", "beta": "SECOND_RESULT"}.get(q, "R")


def _agent(*extra_tools: Any) -> ClioReAct:
    return ClioReAct("question -> answer", tools=[dspy.Tool(_search, name="search"), *extra_tools])


def reference_wire(agent: ClioReAct, events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The adapter formatting ``[head, *events]`` with NO current inputs -- the
    composition the loop sends, rebuilt from the reference events."""
    adapter = _lenient_chat_adapter_cls()()
    head = {"question": QUESTION, "tools": list(agent.tools.values())}
    inputs = {"history": dspy.History(messages=[head, *events])}
    with dspy.context(adapter=adapter):
        return adapter.format(agent.react.signature, [], inputs)


def plane_wire(arc: Any, agent: ClioReAct) -> list[dict[str, Any]]:
    """The wire the loop would send next, read from the plane (``read_steps``)."""
    return reference_wire(agent, read_steps(arc, SESSION, SCOPE))


def _populate(arc: Any, steps: list[dict[str, Any]]) -> None:
    """Append the segments ``StepRecorder`` writes per step (thought, tool_call with id,
    observation with call_id)."""
    for i, s in enumerate(steps):
        call_id = f"call_{i}_0"
        arc.append_segment(SESSION, SCOPE, "thought", {"text": s["thought"]}, step=i)
        arc.append_segment(
            SESSION,
            SCOPE,
            "tool_call",
            {"id": call_id, "name": s["tool_name"], "args": s["tool_args"]},
            step=i,
        )
        arc.append_segment(
            SESSION,
            SCOPE,
            "observation",
            {"call_id": call_id, "text": s["observation"], "is_error": False},
            step=i,
        )


_STEPS = [
    {
        "thought": "search first",
        "tool_name": "search",
        "tool_args": {"q": "alpha"},
        "observation": "SEARCH_RESULT",
    },
    {
        "thought": "again",
        "tool_name": "search",
        "tool_args": {"q": "beta"},
        "observation": "SECOND_RESULT",
    },
]


def _script(steps: list[dict[str, Any]], answer: str = "FINAL") -> list[dict[str, Any]]:
    """The LM script replaying ``steps`` then submitting."""
    return [
        *(
            {
                "next_thought": s["thought"],
                "tool_calls": {"tool_calls": [{"name": s["tool_name"], "args": s["tool_args"]}]},
            }
            for s in steps
        ),
        {
            "next_thought": "done",
            "tool_calls": {"tool_calls": [{"name": "submit", "args": {"answer": answer}}]},
        },
    ]


def _run_loop(arc: Any, agent: ClioReAct, script: list[dict[str, Any]]) -> tuple[Any, DummyLM]:
    adapter = _lenient_chat_adapter_cls()()
    lm = DummyLM(script, adapter=adapter)
    with live_plane_context(arc, session=SESSION, scope=SCOPE):
        with dspy.context(lm=lm, adapter=adapter):
            pred = agent(question=QUESTION)
    return pred, lm


def _wires(lm: DummyLM) -> list[list[dict[str, Any]]]:
    return [entry["messages"] for entry in lm.history]


def _text(wire: list[dict[str, Any]]) -> str:
    return "\n".join(str(m.get("content") or "") for m in wire)


# ---- byte-equality against the reference ------------------------------------------


def test_unedited_fold_matches_reference(arc):
    """``fold_steps`` over an unedited 2-step plane reproduces the reference event list
    EXACTLY (keys, values, order, call ids). Reordering or mutating a folded event turns
    this red."""
    _populate(arc, _STEPS)
    assert fold_steps(arc.render_segments(SESSION, SCOPE)) == expected_history_messages(_STEPS)


def test_every_loop_call_is_byte_equal_to_the_reference(arc):
    """What the REAL loop sent on call ``k`` is byte-identical to the adapter formatting
    ``[head, *reference[:k]]`` -- the plane-sourced context adds no bytes of its own."""
    agent = _agent()
    pred, lm = _run_loop(arc, agent, _script(_STEPS))

    assert (pred.answer, pred.termination_reason) == ("FINAL", "submit")
    reference = expected_history_messages(_STEPS)
    wires = _wires(lm)
    assert len(wires) == len(_STEPS) + 1
    for k, wire in enumerate(wires):
        assert wire == reference_wire(agent, reference[:k]), f"call {k} diverged"


def test_plane_fold_equals_the_loops_own_history(arc):
    """The ARC fold of the plane after the turn == the History the loop kept (minus the
    task-input head): the loop's context and its record are one thing."""
    agent = _agent()
    pred, _ = _run_loop(arc, agent, _script(_STEPS))
    with live_plane_context(arc, session=SESSION, scope=SCOPE):
        folded = read_steps(arc, SESSION, SCOPE)
    assert pred.history.messages[0] == {"question": QUESTION}
    assert folded == pred.history.messages[1:]


def test_consecutive_calls_are_strict_prefix_extensions(arc):
    """Every call is the previous call plus the new step's messages, beneath ONE
    byte-identical closing message (the append-only wire)."""
    _, lm = _run_loop(arc, _agent(), _script(_STEPS))
    wires = _wires(lm)
    tail = wires[0][-1]
    assert "Respond with the corresponding output fields" in str(tail["content"])
    for prev, cur in zip(wires, wires[1:], strict=False):
        assert cur[: len(prev) - 1] == prev[:-1]
        assert cur[-1] == tail
        # one step = its assistant message + its tool-results user message
        assert len(cur) == len(prev) + 2
        assert cur[len(prev) - 1]["role"] == "assistant"
        assert "tool_call_results" in str(cur[len(prev)]["content"])


# ---- the formalized deviations -----------------------------------------------------


def test_deviation_a_inputs_folded_into_head(arc):
    """(a): the wire embeds the ``question`` ONCE at the HEAD (as stock ``dspy.ReActV2``
    embeds the input in the first event) and carries NO moving trailing ``question``
    block. Pinned against REAL stock V2 as the foil."""
    stock = dspy.ReActV2("question -> answer", tools=[dspy.Tool(lambda q: "R", name="search")])
    lm = DummyLM(
        [
            {
                "next_thought": "t0",
                "tool_calls": {"tool_calls": [{"name": "search", "args": {"q": "x"}}]},
            },
            {
                "next_thought": "t1",
                "tool_calls": {"tool_calls": [{"name": "submit", "args": {"answer": "A"}}]},
            },
        ]
    )
    with dspy.context(lm=lm, adapter=dspy.ChatAdapter()):
        pred = stock(question=QUESTION)
    # Stock embeds the input in the FIRST history event...
    assert "question" in pred.history.messages[0]

    # ...and so does clio: the question rides the head user message exactly once, the
    # head renders no assistant turn, and the ONLY trailing block is the static closing.
    _, clio_lm = _run_loop(arc, _agent(), _script(_STEPS))
    for wire in _wires(clio_lm):
        assert [m["role"] for m in wire[:2]] == ["system", "user"]
        assert f"[[ ## question ## ]]\n{QUESTION}" in str(wire[1]["content"])
        assert "[[ ## tools ## ]]" in str(wire[1]["content"])
        assert sum(QUESTION in str(m.get("content") or "") for m in wire) == 1
        assert "[[ ## question ## ]]" not in str(wire[-1]["content"])
    first_call = _wires(clio_lm)[0]
    assert [m["role"] for m in first_call] == ["system", "user", "user"]


def test_deviation_b_summary_surfaces_as_next_thought(arc):
    """(b): a compaction ``summary`` segment surfaces as its own ``next_thought`` event
    so its content still reaches the wire -- pinned through the ARC read seam."""
    arc.append_segment(SESSION, SCOPE, "summary", {"text": "COMPACTED_STATE"}, step=0)
    assert read_steps(arc, SESSION, SCOPE) == [{"next_thought": "COMPACTED_STATE"}]


# ---- prefix property + mutation propagation ----------------------------------------


def test_wire_prefix_is_byte_stable_across_appends(arc):
    """An append-only ARC write keeps the prior wire (minus its static tail) as a
    byte-identical leading prefix of the next (KV-reuse precondition)."""
    agent = _agent()
    _populate(arc, _STEPS[:1])
    first = plane_wire(arc, agent)
    arc.append_segment(SESSION, SCOPE, "thought", {"text": "APPENDED"}, step=1)
    arc.append_segment(
        SESSION, SCOPE, "tool_call", {"id": "call_1_0", "name": "search", "args": {}}, step=1
    )
    arc.append_segment(
        SESSION,
        SCOPE,
        "observation",
        {"call_id": "call_1_0", "text": "APPENDED_OBS", "is_error": False},
        step=1,
    )
    second = plane_wire(arc, agent)
    assert second[: len(first) - 1] == first[:-1]
    assert second[-1] == first[-1]
    assert len(second) == len(first) + 2


def test_out_of_band_delete_changes_the_next_call(arc):
    """THE killer test at the real loop: a segment deleted from the plane mid-turn is
    absent from the loop's NEXT model call -- a loop reading its own in-memory History
    would still send it."""

    def prune() -> str:
        """Deletes the first observation from the live plane."""
        first_obs = next(s for s in arc.render_segments(SESSION, SCOPE) if s.kind == "observation")
        arc.delete_segments(SESSION, SCOPE, [first_obs.id])
        return "PRUNED"

    script = [
        *_script(_STEPS[:1])[:-1],
        {"next_thought": "prune", "tool_calls": {"tool_calls": [{"name": "prune", "args": {}}]}},
        {
            "next_thought": "done",
            "tool_calls": {"tool_calls": [{"name": "submit", "args": {"answer": "F"}}]},
        },
    ]
    _, lm = _run_loop(arc, _agent(dspy.Tool(prune)), script)
    wires = _wires(lm)
    assert "SEARCH_RESULT" in _text(wires[1])
    assert "SEARCH_RESULT" not in _text(wires[2])
    assert "PRUNED" in _text(wires[2])
    assert "search first" in _text(wires[2])  # the step's thought survives


def test_delete_propagates_absent_on_the_wire(arc):
    """A deleted segment vanishes from the next plane-rendered wire."""
    agent = _agent()
    _populate(arc, _STEPS)
    before = plane_wire(arc, agent)
    obs = [s for s in arc.render_segments(SESSION, SCOPE) if s.kind == "observation"]
    arc.delete_segments(SESSION, SCOPE, [obs[-1].id])  # delete the SECOND step's obs
    after = plane_wire(arc, agent)
    assert "SECOND_RESULT" in _text(before)
    assert "SECOND_RESULT" not in _text(after)
    assert "SEARCH_RESULT" in _text(after)  # the first step survives


def test_summarize_propagates_on_the_wire(arc):
    """An ARC summarize op (the sole prefix-reset author) replaces the working set on the
    next rendered wire."""
    agent = _agent()
    _populate(arc, _STEPS[:1])
    ids = [s.id for s in arc.render_segments(SESSION, SCOPE)]
    before = plane_wire(arc, agent)
    arc.summarize_segments(SESSION, SCOPE, ids, {"text": "SUMMARY_REPLACES_ALL"})
    after = plane_wire(arc, agent)
    assert "SEARCH_RESULT" in _text(before) and "SUMMARY_REPLACES_ALL" not in _text(before)
    assert "SUMMARY_REPLACES_ALL" in _text(after) and "SEARCH_RESULT" not in _text(after)
