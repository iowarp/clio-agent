"""Acceptance: the ``ClioReAct`` wire is PROVEN equal to an independently built
reference, and out-of-band ARC edits change the next request.

The reference is :func:`expected_messages` (per step one assistant message -- text +
tool call -- and one tool message with its result, built WITHOUT ``fold_steps``) behind
the static head (the task as one user message). Two invariants are pinned:

* **the ARC fold of the plane == what the loop sent** -- every model call's
  ``Request.messages`` equal ``[head, *expected_messages(steps so far)]``, and the
  loop's own step list equals the fold of what it wrote to the plane;
* **append-only** -- consecutive requests are strict prefix extensions (call ``n``'s
  messages are the leading messages of call ``n + 1``) under one unchanged system
  prompt and tool list: the invariant the stateful transports and provider prompt
  caches need.

Formalized shape (NOT silently absorbed):

* (a) the task inputs ride ONCE in the HEAD user message; the tools ride the request's
  native tool list, never the text; ``test_inputs_ride_the_head_once``;
* (b) a summary / orphan observation (no owning tool call) surfaces as its own user
  ``[earlier context]`` message; ``test_summary_surfaces_as_earlier_context``.

Sabotage: reorder or mutate a folded message in ``fold_steps`` → the reference tests go
red (the fold diverges from the fixed, independently-built reference).
"""

from __future__ import annotations

from typing import Any

import dspy
from dspy.lm15 import Message, Request, TextPart, ToolCallPart, ToolResultPart

from clio_agent.gact.agents.clio_react import ClioReAct
from clio_agent.gact.agents.clio_react_record import fold_steps, read_steps
from tests._scripted_engine import Reply, ScriptedEngine, calls, scripted_lm, summarize

from .conftest import live_plane_context

SESSION, SCOPE = "s1", "agentA"
QUESTION = "find alpha"
HEAD = Message(role="user", parts=(TextPart(text=QUESTION),))


# ---- the independent reference -------------------------------------------------


def expected_messages(steps: list[dict[str, Any]]) -> list[Message]:
    """The equality reference: the typed messages a fully populated plane folds to --
    built INDEPENDENTLY of ``fold_steps``.

    Per step: an assistant message (the step's text, then its call ``call_<step>_0``)
    and a tool message carrying that call's result. No task input (that rides the
    head). ``steps`` is a list of ``{thought, tool_name, tool_args, observation}``.
    """
    messages: list[Message] = []
    for i, s in enumerate(steps):
        call_id = f"call_{i}_0"
        messages.append(
            Message(
                role="assistant",
                parts=(
                    TextPart(text=s["thought"]),
                    ToolCallPart(id=call_id, name=s["tool_name"], input=s["tool_args"]),
                ),
            )
        )
        messages.append(
            Message(
                role="tool",
                parts=(
                    ToolResultPart(
                        id=call_id,
                        content=(TextPart(text=s["observation"]),),
                        name=s["tool_name"],
                        is_error=False,
                    ),
                ),
            )
        )
    return messages


def _search(q: str) -> str:
    """A deterministic search tool."""
    return {"alpha": "SEARCH_RESULT", "beta": "SECOND_RESULT"}.get(q, "R")


def _agent(*extra_tools: Any) -> ClioReAct:
    return ClioReAct("question -> answer", tools=[dspy.Tool(_search, name="search"), *extra_tools])


def plane_messages(arc: Any) -> list[Message]:
    """The messages the loop would send next, read from the plane (``read_steps``)."""
    return [HEAD, *read_steps(arc, SESSION, SCOPE)]


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


def _script(steps: list[dict[str, Any]], answer: str = "FINAL") -> list[Reply]:
    """The LM script replaying ``steps`` then submitting."""
    return [
        *(calls((s["tool_name"], s["tool_args"]), text=s["thought"]) for s in steps),
        calls(("submit", {"answer": answer}), text="done"),
    ]


def _run_loop(arc: Any, agent: ClioReAct, script: list[Reply]) -> tuple[Any, ScriptedEngine]:
    lm, engine = scripted_lm(script)
    with live_plane_context(arc, session=SESSION, scope=SCOPE):
        with dspy.context(lm=lm):
            pred = agent(question=QUESTION)
    return pred, engine


def _text(request_or_messages: Request | list[Message]) -> str:
    messages = (
        request_or_messages.messages
        if isinstance(request_or_messages, Request)
        else request_or_messages
    )
    return str(summarize(messages))


# ---- equality against the reference ------------------------------------------------


def test_unedited_fold_matches_reference(arc):
    """``fold_steps`` over an unedited 2-step plane reproduces the reference messages
    EXACTLY (roles, parts, order, call ids). Reordering or mutating a folded message
    turns this red."""
    _populate(arc, _STEPS)
    assert fold_steps(arc.render_segments(SESSION, SCOPE)) == expected_messages(_STEPS)


def test_every_loop_call_is_equal_to_the_reference(arc):
    """What the REAL loop sent on call ``k`` equals ``[head, *reference[:2k]]`` -- the
    plane-sourced context adds nothing of its own."""
    pred, engine = _run_loop(arc, _agent(), _script(_STEPS))

    assert (pred.answer, pred.termination_reason) == ("FINAL", "submit")
    reference = expected_messages(_STEPS)
    assert len(engine.requests) == len(_STEPS) + 1
    for k, request in enumerate(engine.requests):
        assert list(request.messages) == [HEAD, *reference[: 2 * k]], f"call {k} diverged"


def test_plane_fold_equals_the_loops_own_steps(arc):
    """The ARC fold of the plane after the turn == the step list the loop kept (minus
    the task head): the loop's context and its record are one thing."""
    pred, _ = _run_loop(arc, _agent(), _script(_STEPS))
    with live_plane_context(arc, session=SESSION, scope=SCOPE):
        folded = read_steps(arc, SESSION, SCOPE)
    assert pred.messages[0] == HEAD
    assert folded == pred.messages[1:]


def test_consecutive_calls_are_strict_prefix_extensions(arc):
    """Every request is the previous request plus the new step's two messages, under
    one unchanged system prompt and tool list (the append-only wire)."""
    _, engine = _run_loop(arc, _agent(), _script(_STEPS))
    requests = engine.requests
    for prev, cur in zip(requests, requests[1:], strict=False):
        assert cur.messages[: len(prev.messages)] == prev.messages
        # one step = its assistant message + its tool-results message
        assert len(cur.messages) == len(prev.messages) + 2
        assert [m.role for m in cur.messages[len(prev.messages) :]] == ["assistant", "tool"]
        assert (cur.system, cur.tools) == (prev.system, prev.tools)


# ---- the formalized shape ----------------------------------------------------------


def test_inputs_ride_the_head_once(arc):
    """(a): the ``question`` rides ONCE, in the head user message; the tools ride the
    request's native tool list and are never rendered into the text."""
    _, engine = _run_loop(arc, _agent(), _script(_STEPS))
    for request in engine.requests:
        assert request.messages[0] == HEAD
        assert _text(request).count(QUESTION) == 1
        assert [t.name for t in request.tools] == ["search", "submit"]
        assert "submit" not in str(summarize(request.messages[:1]))


def test_summary_surfaces_as_earlier_context(arc):
    """(b): a compaction ``summary`` segment surfaces as its own user message so its
    content still reaches the wire -- pinned through the ARC read seam."""
    arc.append_segment(SESSION, SCOPE, "summary", {"text": "COMPACTED_STATE"}, step=0)
    assert summarize(read_steps(arc, SESSION, SCOPE)) == [
        ("user", [("text", "[earlier context]\nCOMPACTED_STATE")])
    ]


# ---- prefix property + mutation propagation ----------------------------------------


def test_wire_prefix_is_stable_across_appends(arc):
    """An append-only ARC write keeps the prior messages as the exact leading prefix of
    the next (the KV-reuse precondition)."""
    _populate(arc, _STEPS[:1])
    first = plane_messages(arc)
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
    second = plane_messages(arc)
    assert second[: len(first)] == first
    assert len(second) == len(first) + 2


def test_out_of_band_delete_changes_the_next_call(arc):
    """THE killer test at the real loop: a segment deleted from the plane mid-turn is
    absent from the loop's NEXT model call -- a loop reading its own in-memory steps
    would still send it."""

    def prune() -> str:
        """Deletes the first observation from the live plane."""
        first_obs = next(s for s in arc.render_segments(SESSION, SCOPE) if s.kind == "observation")
        arc.delete_segments(SESSION, SCOPE, [first_obs.id])
        return "PRUNED"

    script = [
        *_script(_STEPS[:1])[:-1],
        calls(("prune", {}), text="prune"),
        calls(("submit", {"answer": "F"}), text="done"),
    ]
    _, engine = _run_loop(arc, _agent(dspy.Tool(prune)), script)
    requests = engine.requests
    assert "SEARCH_RESULT" in _text(requests[1])
    assert "SEARCH_RESULT" not in _text(requests[2])
    assert "PRUNED" in _text(requests[2])
    assert "search first" in _text(requests[2])  # the step's thought survives


def test_delete_propagates_absent_on_the_wire(arc):
    """A deleted segment vanishes from the next plane-rendered messages."""
    _populate(arc, _STEPS)
    before = plane_messages(arc)
    obs = [s for s in arc.render_segments(SESSION, SCOPE) if s.kind == "observation"]
    arc.delete_segments(SESSION, SCOPE, [obs[-1].id])  # delete the SECOND step's obs
    after = plane_messages(arc)
    assert "SECOND_RESULT" in _text(before)
    assert "SECOND_RESULT" not in _text(after)
    assert "SEARCH_RESULT" in _text(after)  # the first step survives


def test_summarize_propagates_on_the_wire(arc):
    """An ARC summarize op (the sole prefix-reset author) replaces the working set on the
    next rendered messages."""
    _populate(arc, _STEPS[:1])
    ids = [s.id for s in arc.render_segments(SESSION, SCOPE)]
    before = plane_messages(arc)
    arc.summarize_segments(SESSION, SCOPE, ids, {"text": "SUMMARY_REPLACES_ALL"})
    after = plane_messages(arc)
    assert "SEARCH_RESULT" in _text(before) and "SUMMARY_REPLACES_ALL" not in _text(before)
    assert "SUMMARY_REPLACES_ALL" in _text(after) and "SEARCH_RESULT" not in _text(after)
