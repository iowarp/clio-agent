"""Loop-contract pins for ``ClioReAct`` (:mod:`clio_agent.gact.agents.clio_react`).

They pin three properties the design calls out:

1. **Append-only composition** (design §3): across scripted steps every request's
   system prompt and tool list are byte-identical, and its messages are a strict
   prefix of the next request's -- one static task head, then one assistant + tool
   message pair per committed step (the prompt-cache / session-delta precondition).
   The returned ``Prediction.messages`` is that same list.
2. **Thinking stays thinking** (design §4, risk 1): the provider's thinking rides a
   typed ``ThinkingPart`` on the step it came with; it never becomes the answer text
   and the visible text never becomes thinking.
3. **workflow_state on submit args** (design fact 4): a typed ``workflow_state``
   output field on the user signature rides the ``submit`` tool's ``arg_types`` and
   its native tool schema unchanged (the load-bearing typed extract survives).
"""

from __future__ import annotations

from typing import Any

import dspy
import pytest
from dspy.lm15 import TextPart, ThinkingPart

from clio_agent.gact.agents.clio_react import TOOL_USE_NOTE, ClioReAct
from tests._scripted_engine import Reply, ScriptedEngine, calls, scripted_lm

pytestmark = pytest.mark.usefixtures("clio_core_plane")


def _search(q: str) -> str:
    """A trivial deterministic tool."""
    return "SEARCH_RESULT"


def _two_steps_then_submit() -> list[Reply]:
    return [
        calls(("search", {"q": "x"}), text="t0"),
        calls(("search", {"q": "y"}), text="t1"),
        calls(("submit", {"answer": "FINAL"}), text="t2"),
    ]


def _build_agent(signature: Any = "question -> answer") -> ClioReAct:
    return ClioReAct(signature, tools=[dspy.Tool(_search, name="search")], max_iters=6)


def _run(
    script: list[Reply], signature: Any = "question -> answer"
) -> tuple[dspy.Prediction, ScriptedEngine]:
    lm, engine = scripted_lm(script)
    with dspy.context(lm=lm):
        return _build_agent(signature)(question="find alpha"), engine


# --- 1. append-only composition ------------------------------------------------


def test_requests_grow_append_only_behind_a_static_head() -> None:
    """Each request's messages are a strict prefix of the next one's; the system
    prompt and the tools never change bytes within a forward."""
    pred, engine = _run(_two_steps_then_submit())

    assert (pred.answer, pred.termination_reason) == ("FINAL", "submit")
    requests = engine.requests
    # call 0 sees only the head (the tool-use note, then the question), then one
    # (assistant, tool) pair per prior step.
    assert [len(r.messages) for r in requests] == [2, 4, 6]
    note, head = requests[0].messages
    tool_note = TextPart(text=f"[clio: tool_use]\n{TOOL_USE_NOTE}")
    assert (note.role, note.parts) == ("user", (tool_note,))
    assert (head.role, head.parts) == ("user", (TextPart(text="find alpha"),))
    assert [t.name for t in requests[0].tools] == ["search", "submit"]
    for earlier, later in zip(requests, requests[1:], strict=False):
        assert later.messages[: len(earlier.messages)] == earlier.messages
        assert (later.system, later.tools) == (earlier.system, earlier.tools)


def test_prediction_messages_are_the_wire_plus_the_final_step() -> None:
    """The Prediction's messages extend the last request by the step it answered."""
    pred, engine = _run(_two_steps_then_submit())

    last = engine.requests[-1].messages
    assert list(pred.messages[: len(last)]) == list(last)
    final_step, final_results = pred.messages[len(last) :]
    assert [type(p).__name__ for p in final_step.parts] == ["TextPart", "ToolCallPart"]
    assert final_results.role == "tool"


# --- 2. thinking stays thinking -------------------------------------------------


def test_thinking_is_a_typed_part_never_the_answer() -> None:
    pred, engine = _run(
        [
            calls(("search", {"q": "x"}), text="checking", thinking="private plan"),
            Reply(text="the answer", thinking="final musing"),
        ]
    )

    assert pred.answer == "the answer"
    step = engine.requests[1].messages[2]
    assert [type(p) for p in step.parts[:2]] == [ThinkingPart, TextPart]
    assert (step.parts[0].text, step.parts[1].text) == ("private plan", "checking")
    final = pred.messages[-1]
    assert final.parts == (ThinkingPart(text="final musing"), TextPart(text="the answer"))


# --- 3. workflow_state rides the submit tool's typed args ----------------------


class _Sig(dspy.Signature):
    question: str = dspy.InputField()
    answer: str = dspy.OutputField()
    workflow_state: dict[str, Any] = dspy.OutputField()


def test_workflow_state_rides_submit_tool_arg_types() -> None:
    """A typed ``workflow_state`` output field becomes a typed arg of the ``submit``
    tool (design fact 4), and the model sees it in the native tool schema."""
    submit = _build_agent(_Sig).tools["submit"]
    assert submit.arg_types["workflow_state"] == dict[str, Any]
    assert submit.arg_types["answer"] is str

    _, engine = _run([Reply(text="n/a")], signature=_Sig)
    [schema] = [t.parameters for t in engine.requests[0].tools if t.name == "submit"]
    assert set(schema["properties"]) == {"answer", "workflow_state"}
    assert schema["properties"]["workflow_state"]["type"] == "object"


def test_submit_returns_typed_workflow_state_value() -> None:
    """End-to-end: the model's ``submit`` call carries the typed ``workflow_state``
    value through to the final Prediction."""
    pred, _ = _run(
        [
            calls(
                ("submit", {"answer": "DONE", "workflow_state": {"status": "complete"}}),
                text="submit now",
            )
        ],
        signature=_Sig,
    )
    assert pred.answer == "DONE"
    assert pred.workflow_state == {"status": "complete"}
