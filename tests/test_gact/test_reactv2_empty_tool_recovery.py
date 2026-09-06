"""Direct-response contract for tool-free ReActV2 model output."""

from __future__ import annotations

from typing import Any

import dspy
import pytest

from clio_agent.gact.agents.reactv2 import retaining_reactv2_cls


def _agent(*, max_iters: int = 0) -> Any:
    return retaining_reactv2_cls()(
        "question -> answer",
        tools=[dspy.Tool(lambda q: f"result:{q}", name="search")],
        max_iters=max_iters,
    )


def test_tool_free_prose_is_the_direct_answer_after_one_model_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Not choosing a tool is ordinary completion, not a repair condition."""
    agent = _agent()
    calls: list[dict[str, Any]] = []

    def react(**kwargs: Any) -> dspy.Prediction:
        calls.append(kwargs)
        return dspy.Prediction(next_thought="Ready.", tool_calls={"tool_calls": []})

    monkeypatch.setattr(agent, "react", react)
    prediction = agent(question="Reply ready. Do not call tools.")

    assert len(calls) == 1
    assert prediction.answer == "Ready."
    assert prediction.termination_reason == "direct_response"
    assert prediction.history.messages[0]["next_thought"] == "Ready."


def test_blank_tool_free_response_completes_without_resampling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ReAct path does not semantically classify even blank model prose."""
    agent = _agent()
    calls = 0

    def react(**_kwargs: Any) -> dspy.Prediction:
        nonlocal calls
        calls += 1
        return dspy.Prediction(next_thought="", tool_calls={"tool_calls": []})

    monkeypatch.setattr(agent, "react", react)
    prediction = agent(question="hello")

    assert calls == 1
    assert prediction.answer == ""
    assert prediction.termination_reason == "direct_response"


def test_model_can_use_a_tool_then_finish_with_plain_prose(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A later tool-free response ends the same loop without a hidden submit."""
    observed: list[dict[str, Any]] = []
    tool_calls: list[str] = []

    def search(q: str) -> str:
        tool_calls.append(q)
        return "SEARCH_RESULT"

    agent = retaining_reactv2_cls()(
        "question -> answer",
        tools=[dspy.Tool(search)],
        max_iters=0,
    )

    def react(**kwargs: Any) -> dspy.Prediction:
        observed.append(kwargs)
        if len(observed) == 1:
            return dspy.Prediction(
                next_thought="I will check.",
                tool_calls={"tool_calls": [{"name": "search", "args": {"q": "grounded"}}]},
            )
        return dspy.Prediction(
            next_thought="The grounded answer is complete.",
            tool_calls={"tool_calls": []},
        )

    monkeypatch.setattr(agent, "react", react)
    prediction = agent(question="find it")

    assert len(observed) == 2
    assert tool_calls == ["grounded"]
    assert prediction.answer == "The grounded answer is complete."
    assert prediction.termination_reason == "direct_response"


def test_iteration_cap_does_not_trigger_an_out_of_loop_model_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An explicit runaway cap stops rather than invoking a forced-submit tail."""
    agent = _agent(max_iters=1)
    calls = 0

    def react(**_kwargs: Any) -> dspy.Prediction:
        nonlocal calls
        calls += 1
        return dspy.Prediction(
            next_thought="still working",
            tool_calls={"tool_calls": [{"name": "search", "args": {"q": "x"}}]},
        )

    monkeypatch.setattr(agent, "react", react)
    prediction = agent(question="find it")

    assert calls == 1
    assert "answer" not in prediction
    assert prediction.termination_reason == "max_iters"
