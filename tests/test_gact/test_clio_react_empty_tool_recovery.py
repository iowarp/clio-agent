"""Direct-response contract for tool-free ``ClioReAct`` model output."""

from __future__ import annotations

import dspy
from dspy.lm15 import TextPart

from clio_agent.gact.agents.clio_react import ClioReAct
from tests._scripted_engine import Reply, calls, scripted_lm


def _agent(*, max_iters: int = 0, tool: dspy.Tool | None = None) -> ClioReAct:
    return ClioReAct(
        "question -> answer",
        tools=[tool or dspy.Tool(lambda q: f"result:{q}", name="search")],
        max_iters=max_iters,
    )


def test_tool_free_prose_is_the_direct_answer_after_one_model_call() -> None:
    """Not choosing a tool is ordinary completion, not a repair condition."""
    lm, engine = scripted_lm([Reply(text="Ready.")])
    with dspy.context(lm=lm):
        prediction = _agent()(question="Reply ready. Do not call tools.")

    assert len(engine.requests) == 1
    assert prediction.answer == "Ready."
    assert prediction.termination_reason == "direct_response"
    # the messages are the task head, then one assistant message per step
    head, step = prediction.messages
    assert (head.role, head.parts) == ("user", (TextPart(text="Reply ready. Do not call tools."),))
    assert (step.role, step.parts) == ("assistant", (TextPart(text="Ready."),))


def test_blank_tool_free_response_completes_without_resampling() -> None:
    """The loop does not semantically classify even blank model prose."""
    lm, engine = scripted_lm([Reply(text="")])
    with dspy.context(lm=lm):
        prediction = _agent()(question="hello")

    assert len(engine.requests) == 1
    assert prediction.answer == ""
    assert prediction.termination_reason == "direct_response"


def test_model_can_use_a_tool_then_finish_with_plain_prose() -> None:
    """A later tool-free response ends the same loop without a hidden submit."""
    tool_calls: list[str] = []

    def search(q: str) -> str:
        tool_calls.append(q)
        return "SEARCH_RESULT"

    lm, engine = scripted_lm(
        [
            calls(("search", {"q": "grounded"}), text="I will check."),
            Reply(text="The grounded answer is complete."),
        ]
    )
    with dspy.context(lm=lm):
        prediction = _agent(tool=dspy.Tool(search))(question="find it")

    assert len(engine.requests) == 2
    assert tool_calls == ["grounded"]
    assert prediction.answer == "The grounded answer is complete."
    assert prediction.termination_reason == "direct_response"


def test_iteration_cap_does_not_trigger_an_out_of_loop_model_call() -> None:
    """An explicit runaway cap stops rather than invoking a forced-submit tail."""
    lm, engine = scripted_lm([calls(("search", {"q": "x"}), text="still working")] * 3)
    with dspy.context(lm=lm):
        prediction = _agent(max_iters=1)(question="find it")

    assert len(engine.requests) == 1
    assert "answer" not in prediction
    assert prediction.termination_reason == "max_iters"
