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


def test_recovered_empty_tool_blip_does_not_poison_a_later_max_iters_exhaustion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A RECOVERED tool-less blip must not relabel a later, healthy cap exhaustion.

    A blueprint-declared ``max_iters`` cap is an ordinary runaway backstop, not a
    provider protocol failure. Exhausting it after the model already recovered
    from an early empty-tool response must terminate as ``max_iters`` and carry
    no ``provider_protocol_error`` payload -- otherwise the turn settles failed
    and the user is told the provider repeatedly returned no structured call.
    """
    monkeypatch.setenv("CLIO_EMPTY_TOOL_REPAIR_ATTEMPTS", "1")
    monkeypatch.setenv("CLIO_SUBMIT_REPAIR_ATTEMPTS", "0")
    from clio_agent import conf

    conf.reload()
    agent = retaining_reactv2_cls()(
        "question -> answer",
        tools=[dspy.Tool(lambda q: q, name="search")],
        max_iters=3,
    )
    responses: list[dspy.Prediction] = [
        # Iteration 0: a tool-less blip -- recovered by the bounded retry.
        dspy.Prediction(next_thought="still planning", tool_calls={"tool_calls": []}),
        # Iterations 1 and 2: healthy tool work that simply never submits.
        dspy.Prediction(
            next_thought="working",
            tool_calls={"tool_calls": [{"name": "search", "args": {"q": "a"}}]},
        ),
        dspy.Prediction(
            next_thought="working",
            tool_calls={"tool_calls": [{"name": "search", "args": {"q": "b"}}]},
        ),
    ]
    seen: list[dict[str, Any]] = []

    def react(**kwargs: Any) -> dspy.Prediction:
        seen.append(kwargs)
        if responses:
            return responses.pop(0)
        return dspy.Prediction(next_thought="done", tool_calls={"tool_calls": []})

    monkeypatch.setattr(agent, "react", react)
    prediction = agent(question="find it")

    # Three loop iterations (0 empty + recovery at 1 and 2) then the forced submit.
    assert len(seen) == 4
    assert prediction.termination_reason == "max_iters"
    assert getattr(prediction, "error_info", None) is None


def test_unresolvable_repair_budget_reports_a_typed_reason(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A discarded operator configuration is a degradation, not a silent default.

    ``conf.as_int`` raises on garbage by design, so a malformed
    ``CLIO_EMPTY_TOOL_REPAIR_ATTEMPTS`` reaches the fallback. The committed
    default still bounds recovery, but the ignored configuration must be
    reported with a typed reason rather than swallowed.
    """
    from clio_agent.gact.agents import reactv2_events

    monkeypatch.setenv("CLIO_EMPTY_TOOL_REPAIR_ATTEMPTS", "not-a-number")
    from clio_agent import conf

    conf.reload()

    with caplog.at_level("WARNING", logger="clio_agent.gact.agents.reactv2_events"):
        assert reactv2_events._empty_tool_repair_attempts() == 3

    assert any(
        "empty_tool_repair_budget_unresolved" in record.message
        and "reason=config_resolve_failed" in record.message
        for record in caplog.records
    ), [record.message for record in caplog.records]
