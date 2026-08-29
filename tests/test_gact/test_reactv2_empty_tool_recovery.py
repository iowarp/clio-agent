"""Recovery contract for provider responses that omit structured ReAct tool calls."""

from __future__ import annotations

from typing import Any

import dspy
import pytest
from dspy.utils.dummies import DummyLM

from clio_agent.gact.agents.reactv2 import retaining_reactv2_cls


def test_empty_tool_response_reasks_with_normal_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    """A tool-less planning response is not mistaken for task completion.

    The provider must emit the structured call itself: CLIO retains the real response,
    re-exposes the normal catalog once, and executes only the subsequent model-produced
    call. No tool name or arguments are inferred from prose.
    """
    monkeypatch.setenv("CLIO_EMPTY_TOOL_REPAIR_ATTEMPTS", "1")
    from clio_agent import conf

    conf.reload()
    calls: list[str] = []

    def search(q: str) -> str:
        calls.append(q)
        return "SEARCH_RESULT"

    agent = retaining_reactv2_cls()(
        "question -> answer",
        tools=[dspy.Tool(search)],
        max_iters=0,
    )
    lm = DummyLM(
        [
            {
                "next_thought": "I need to search for the grounded result.",
                "tool_calls": {"tool_calls": []},
            },
            {
                "next_thought": "Calling the declared tool now.",
                "tool_calls": {"tool_calls": [{"name": "search", "args": {"q": "grounded"}}]},
            },
            {
                "next_thought": "The observed result answers the question.",
                "tool_calls": {"tool_calls": [{"name": "submit", "args": {"answer": "DONE"}}]},
            },
        ]
    )

    with dspy.context(lm=lm, adapter=dspy.ChatAdapter()):
        prediction = agent(question="find it")

    assert prediction.answer == "DONE"
    assert prediction.termination_reason == "submit"
    assert calls == ["grounded"]
    assert prediction.history.messages[0]["next_thought"] == (
        "I need to search for the grounded result."
    )
    assert "tool_calls" not in prediction.history.messages[0]


def test_repeated_empty_tool_responses_stop_at_the_repair_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Malformed provider output cannot turn the recovery path into an infinite loop."""
    monkeypatch.setenv("CLIO_EMPTY_TOOL_REPAIR_ATTEMPTS", "1")
    monkeypatch.setenv("CLIO_SUBMIT_REPAIR_ATTEMPTS", "0")
    from clio_agent import conf

    conf.reload()
    agent = retaining_reactv2_cls()(
        "question -> answer",
        tools=[dspy.Tool(lambda q: q, name="search")],
        max_iters=0,
    )
    observed: list[dict[str, Any]] = []

    def react(**kwargs: Any) -> dspy.Prediction:
        observed.append(kwargs)
        return dspy.Prediction(next_thought="still planning", tool_calls={"tool_calls": []})

    monkeypatch.setattr(agent, "react", react)
    prediction = agent(question="find it")

    # Initial response + exactly one bounded recovery + one forced-submit attempt.
    assert len(observed) == 3
    assert prediction.termination_reason == "empty_tool_calls"
    assert [tool.name for tool in observed[-1]["tools"]] == ["submit"]


def test_default_repair_budget_allows_three_consecutive_provider_reasks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Normal long workflows survive several malformed tool-less provider turns."""
    monkeypatch.delenv("CLIO_EMPTY_TOOL_REPAIR_ATTEMPTS", raising=False)
    monkeypatch.setenv("CLIO_SUBMIT_REPAIR_ATTEMPTS", "0")
    from clio_agent import conf

    conf.reload()
    agent = retaining_reactv2_cls()(
        "question -> answer",
        tools=[dspy.Tool(lambda q: q, name="search")],
        max_iters=0,
    )
    observed: list[dict[str, Any]] = []

    def react(**kwargs: Any) -> dspy.Prediction:
        observed.append(kwargs)
        return dspy.Prediction(next_thought="still planning", tool_calls={"tool_calls": []})

    monkeypatch.setattr(agent, "react", react)
    prediction = agent(question="find it")

    # Initial response + three bounded recoveries + one forced-submit attempt.
    assert len(observed) == 5
    assert prediction.termination_reason == "empty_tool_calls"
