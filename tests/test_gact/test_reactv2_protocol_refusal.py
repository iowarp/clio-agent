"""D1 (#1282, C1-S2): a typed MCP protocol refusal terminates the react loop
FAST instead of looping (the #1275 hang shape).

Upstream ``dspy.predict.react_v2.ReActV2._execute_tool_calls`` catches *any*
tool-callable exception and turns it into a plain string observation the LM
may keep re-triggering — clio's ``instrumented_forward`` loop has no
iteration cap by default (#1226 D1b), so an LM that does not recognize a
refusal as permanent can retry it forever. These tests assert the flow
terminates on the typed terminal outcome instead of continuing to poll/retry
-- bounded by ASSERTING THE BEHAVIOR (the typed exception propagates, and the
LM is invoked at most once), never by asserting wall-clock timing.
"""

from __future__ import annotations

from typing import Any

import dspy
import pytest
from dspy.utils.dummies import DummyLM

from clio_agent.errors import MCPMissingRequiredClientCapabilityError
from clio_agent.gact.agents.reactv2 import retaining_reactv2_cls
from clio_agent.tools.mcp_errors import pop_pending_terminal_refusal


@pytest.fixture(autouse=True)
def _clean_refusal_signal() -> Any:
    """Never let a stray mark from another test leak into this one, or out of it."""

    pop_pending_terminal_refusal()
    yield
    pop_pending_terminal_refusal()


def _refusing_tool_call_count() -> list[int]:
    """A mutable counter closed over by the raising tool (call-count evidence)."""

    return [0]


def _make_refusing_tool(calls: list[int]) -> Any:
    def task_echo(payload: str = "") -> str:
        calls[0] += 1
        raise MCPMissingRequiredClientCapabilityError(
            "task_echo requires the tasks extension",
            {"requiredCapabilities": {"extensions": {"io.modelcontextprotocol/tasks": {}}}},
        )

    return task_echo


def _always_retry_lm() -> DummyLM:
    """Scripts the SAME doomed tool call forever — an LM that never recognizes
    a deterministic refusal is permanent (the #1275 shape). A real, unbounded
    ``max_iters`` would keep drawing from this if the fix did not intervene;
    ten repeats is far more than the ONE call the fix must stop it at."""

    step = {
        "next_thought": "retry task_echo",
        "tool_calls": {"tool_calls": [{"name": "task_echo", "args": {"payload": "ping"}}]},
    }
    return DummyLM([dict(step) for _ in range(10)])


def test_protocol_refusal_terminates_the_loop_on_the_first_call() -> None:
    """RED before the #1282 D1 fix: this raised nothing (dspy swallowed the
    refusal into a string observation) and the loop kept calling ``react``
    until ``max_iters`` (here 6) exhausted into a forced submit -- no typed
    outcome, no fast termination. GREEN after the fix: the typed refusal
    propagates out of ``agent(...)`` and the LM is invoked exactly once."""

    calls = _refusing_tool_call_count()
    cls = retaining_reactv2_cls()
    agent = cls("question -> answer", tools=[dspy.Tool(_make_refusing_tool(calls))], max_iters=6)

    react_call_count = [0]
    original_react = agent.react

    def _counting_react(**kwargs: Any) -> Any:
        react_call_count[0] += 1
        return original_react(**kwargs)

    agent.react = _counting_react  # type: ignore[method-assign]

    with dspy.context(lm=_always_retry_lm(), adapter=dspy.ChatAdapter()):
        with pytest.raises(MCPMissingRequiredClientCapabilityError) as excinfo:
            agent(question="fetch it")

    assert excinfo.value.reason == "mcp_capability_refused"
    # Terminal-fast: the tool ran once, the LM was asked once -- no retry loop.
    assert calls[0] == 1
    assert react_call_count[0] == 1
    # One-shot: nothing leaks past the raise for a later, unrelated call.
    assert pop_pending_terminal_refusal() is None


def test_protocol_refusal_message_names_the_redial_extension() -> None:
    """D2: the propagated error's message carries what to re-dial with."""

    calls = _refusing_tool_call_count()
    cls = retaining_reactv2_cls()
    agent = cls("question -> answer", tools=[dspy.Tool(_make_refusing_tool(calls))], max_iters=6)

    with dspy.context(lm=_always_retry_lm(), adapter=dspy.ChatAdapter()):
        with pytest.raises(MCPMissingRequiredClientCapabilityError) as excinfo:
            agent(question="fetch it")

    assert "io.modelcontextprotocol/tasks" in str(excinfo.value)


def test_ordinary_tool_error_is_not_escalated() -> None:
    """An UNTYPED tool error stays the model's own retryable observation -- the
    fix is scoped strictly to the typed refusal class (superseding principle
    #1: clio is not the router/decider for ordinary tool failures)."""

    def flaky(payload: str = "") -> str:
        raise RuntimeError("transient upstream hiccup")

    cls = retaining_reactv2_cls()
    agent = cls("question -> answer", tools=[dspy.Tool(flaky)], max_iters=6)
    lm = DummyLM(
        [
            {
                "next_thought": "try flaky",
                "tool_calls": {"tool_calls": [{"name": "flaky", "args": {"payload": "x"}}]},
            },
            {
                "next_thought": "give up, submit",
                "tool_calls": {"tool_calls": [{"name": "submit", "args": {"answer": "n/a"}}]},
            },
        ]
    )
    with dspy.context(lm=lm, adapter=dspy.ChatAdapter()):
        pred = agent(question="fetch it")
    assert pred.answer == "n/a"
    assert pop_pending_terminal_refusal() is None
