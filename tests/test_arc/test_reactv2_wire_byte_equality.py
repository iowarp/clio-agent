"""Acceptance: the ReActV2 wire composition is PROVEN byte-for-byte against a recorded
reference, and out-of-band ARC edits change the next V2 prompt (#901 S5).

The V2-side analog of ``test_live_plane_byte_equality.py`` (which pins the CLASSIC path
and stays byte-untouched — the classic path is frozen). Where the classic reference is
``expected_trajectory_dict`` (a trajectory dict) + ``stock_format_trajectory`` (the stock
single-string formatter), the V2 reference is :func:`expected_history_messages` (the
append-only ``dspy.History`` message list) + the stock ``ChatAdapter`` formatter over it.

**The two documented S2 deviations are formalized here as the reference's expected shape,
NOT silently absorbed:**

* (a) the user question is NOT folded into the History prefix — the reference messages
  carry NO ``question`` key (stock ``ReActV2`` embeds it in the first event; clio renders
  it per-call via the adapter's current-input path, keeping the cached prefix free of the
  duplicated question). ``test_deviation_a_question_not_in_prefix`` pins this against the
  REAL stock ``dspy.ReActV2`` as the foil.
* (b) a summary / orphan observation (no owning tool call) surfaces under ``next_thought``.
  ``test_deviation_b_summary_surfaces_as_next_thought`` pins it.

Sabotage tripwire (b, from the task): perturbing the V2 wire composition — reorder or
mutate a folded message in ``segments_to_messages`` — turns
``test_unedited_fold_matches_reference`` and ``test_override_wire_byte_equal_to_reference``
red (the fold diverges from the fixed, independently-built reference).
"""

from __future__ import annotations

from typing import Any

import dspy
from dspy.adapters.types.tool import ToolCallResults, ToolCalls
from dspy.utils.dummies import DummyLM

from clio_agent.gact.agents.reactv2 import _RetainingReActV2, segments_to_messages
from clio_agent.lm.adapters import _lenient_chat_adapter_cls

from .conftest import live_plane_context

SESSION, SCOPE = "s1", "agentA"


# ---- references (the V2 analogs of conftest.expected_trajectory_dict / stock_format) ----


def expected_history_messages(steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The recorded byte-equality reference: the append-only ``dspy.History`` message list
    a fully-populated V2 loop folds to — built INDEPENDENTLY of ``segments_to_messages``.

    Formalizes deviation (a): each event carries ``next_thought`` + ``tool_calls`` ONLY —
    NO ``question`` key (the current input is rendered separately by the adapter, never
    folded into the cached prefix). ``steps`` is a list of
    ``{thought, tool_name, tool_args, observation}``.
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


def _react_signature_and_tools() -> tuple[Any, list[Any]]:
    agent = _RetainingReActV2("question -> answer", tools=[dspy.Tool(lambda q: "R", name="search")])
    return agent.react.signature, list(agent.tools.values())


def stock_wire(
    messages: list[dict[str, Any]], signature: Any, tools: list[Any]
) -> list[dict[str, Any]]:
    """The byte reference: the STOCK ``ChatAdapter`` formatting a pre-folded History (the
    V2 analog of conftest.stock_format_trajectory). No ARC, no override — pure stock."""
    inputs = {"question": "find alpha", "history": dspy.History(messages=messages), "tools": tools}
    with dspy.context(adapter=dspy.ChatAdapter()):
        return dspy.ChatAdapter().format(signature, [], inputs)


def override_wire(signature: Any, tools: list[Any]) -> list[dict[str, Any]]:
    """The wire the clio ``LenientChatAdapter`` override produces from ARC (History
    deliberately empty; ARC must win)."""
    adapter = _lenient_chat_adapter_cls()()
    inputs = {"question": "find alpha", "history": dspy.History(messages=[]), "tools": tools}
    with dspy.context(adapter=adapter):
        return adapter.format(signature, [], inputs)


def _populate(arc: Any, steps: list[dict[str, Any]]) -> None:
    """Append (thought, tool_call, observation) segments for each turn (simulating the
    loop's ARC writes)."""
    for i, s in enumerate(steps):
        arc.append_segment(SESSION, SCOPE, "thought", {"text": s["thought"]}, step=i)
        arc.append_segment(
            SESSION, SCOPE, "tool_call", {"name": s["tool_name"], "args": s["tool_args"]}, step=i
        )
        arc.append_segment(SESSION, SCOPE, "observation", {"text": s["observation"]}, step=i)


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


# ---- byte-equality against the recorded reference ----------------------------


def test_unedited_fold_matches_reference(arc):
    """``segments_to_messages`` over an unedited 2-turn plane reproduces the recorded
    reference message list EXACTLY (keys, values, order, tool-result ids). Reordering or
    mutating a folded message turns this red (sabotage b)."""
    _populate(arc, _STEPS)
    with live_plane_context(arc, session=SESSION, scope=SCOPE):
        folded = segments_to_messages(arc.render_segments(SESSION, SCOPE))
    assert folded == expected_history_messages(_STEPS)


def test_override_wire_byte_equal_to_reference(arc):
    """The clio adapter override renders the ARC plane onto the wire BYTE-FOR-BYTE
    identically to the stock ChatAdapter formatting the recorded reference message list.

    The stock reference is computed from the INDEPENDENT ``expected_history_messages`` — so
    a perturbation of the fold changes the override wire but not the reference, turning this
    red (sabotage b at the wire level)."""
    signature, tools = _react_signature_and_tools()
    reference = stock_wire(expected_history_messages(_STEPS), signature, tools)
    _populate(arc, _STEPS)
    with live_plane_context(arc, session=SESSION, scope=SCOPE):
        produced = override_wire(signature, tools)
    assert produced == reference
    assert any("SEARCH_RESULT" in str(m.get("content") or "") for m in produced)


def test_override_delegates_to_stock_formatter_over_same_fold(arc):
    """The override output equals the stock formatter over the SAME ARC-sourced fold — the
    override adds NO bytes of its own (the V2 analog of the classic byte-equality)."""
    signature, tools = _react_signature_and_tools()
    _populate(arc, _STEPS)
    with live_plane_context(arc, session=SESSION, scope=SCOPE):
        folded = segments_to_messages(arc.render_segments(SESSION, SCOPE))
        produced = override_wire(signature, tools)
    stock = stock_wire(folded, signature, tools)
    assert produced == stock


# ---- the formalized deviations, pinned against the stock foil -----------------


def test_deviation_a_question_not_in_prefix():
    """Deviation (a): stock ``dspy.ReActV2`` embeds the user question in its FIRST history
    event; clio's folded prefix carries NO ``question`` key. Pinned against REAL stock V2."""
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
        pred = stock(question="find alpha")
    # Stock embeds the input in the first event...
    assert "question" in pred.history.messages[0]
    # ...clio's fold reference never does (the current input rides the adapter per-call).
    assert all("question" not in m for m in expected_history_messages(_STEPS))


def test_deviation_b_summary_surfaces_as_next_thought(arc):
    """Deviation (b): a compaction ``summary`` segment (rendered as an observation with no
    owning tool call) surfaces under ``next_thought`` so its content still reaches the
    wire — pinned end-to-end through the ARC read seam."""
    arc.append_segment(SESSION, SCOPE, "summary", {"text": "COMPACTED_STATE"}, step=0)
    with live_plane_context(arc, session=SESSION, scope=SCOPE):
        folded = segments_to_messages(arc.render_segments(SESSION, SCOPE))
    assert folded == [{"next_thought": "COMPACTED_STATE"}]


# ---- prefix property (the #891 wire fingerprint) + mutation propagation -------


def test_wire_prefix_is_byte_stable_across_appends(arc):
    """The #891 fingerprint at the wire level: an append-only ARC write keeps the prior
    rendered wire messages as a byte-identical leading prefix (KV-reuse precondition)."""
    signature, tools = _react_signature_and_tools()
    _populate(arc, _STEPS[:1])
    with live_plane_context(arc, session=SESSION, scope=SCOPE):
        first = override_wire(signature, tools)
        arc.append_segment(SESSION, SCOPE, "thought", {"text": "APPENDED"}, step=1)
        arc.append_segment(SESSION, SCOPE, "tool_call", {"name": "search", "args": {}}, step=1)
        arc.append_segment(SESSION, SCOPE, "observation", {"text": "APPENDED_OBS"}, step=1)
        second = override_wire(signature, tools)
    # The committed history messages of `first` (all but the trailing current-input block)
    # are carried into `second` byte-for-byte.
    committed = first[:-1]
    assert second[: len(committed)] == committed
    assert len(second) > len(first)


def test_delete_propagates_absent_on_the_wire(arc):
    """THE killer test (V2 wire): a deleted segment vanishes from the next rendered wire —
    a shadow-of-an-internal-History implementation would still show it."""
    signature, tools = _react_signature_and_tools()
    _populate(arc, _STEPS)
    with live_plane_context(arc, session=SESSION, scope=SCOPE):
        before = override_wire(signature, tools)
        obs = [s for s in arc.render_segments(SESSION, SCOPE) if s.kind == "observation"]
        arc.delete_segments(SESSION, SCOPE, [obs[-1].id])  # delete the SECOND turn's obs
        after = override_wire(signature, tools)
    before_text = "\n".join(str(m.get("content") or "") for m in before)
    after_text = "\n".join(str(m.get("content") or "") for m in after)
    assert "SECOND_RESULT" in before_text
    assert "SECOND_RESULT" not in after_text
    assert "SEARCH_RESULT" in after_text  # the first turn survives


def test_summarize_propagates_on_the_wire(arc):
    """An ARC summarize op (the sole prefix-reset author) replaces the working set on the
    next rendered wire."""
    signature, tools = _react_signature_and_tools()
    _populate(arc, _STEPS[:1])
    with live_plane_context(arc, session=SESSION, scope=SCOPE):
        ids = [s.id for s in arc.render_segments(SESSION, SCOPE)]
        before = override_wire(signature, tools)
        arc.summarize_segments(SESSION, SCOPE, ids, {"text": "SUMMARY_REPLACES_ALL"})
        after = override_wire(signature, tools)
    before_text = "\n".join(str(m.get("content") or "") for m in before)
    after_text = "\n".join(str(m.get("content") or "") for m in after)
    assert "SEARCH_RESULT" in before_text and "SUMMARY_REPLACES_ALL" not in before_text
    assert "SUMMARY_REPLACES_ALL" in after_text and "SEARCH_RESULT" not in after_text
