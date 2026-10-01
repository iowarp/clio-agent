"""D1 (#1282, C1-S2): a typed MCP protocol refusal terminates the ``ClioReAct`` loop
FAST instead of looping (the #1275 hang shape).

A tool-callable exception is normally the model's own observation, and the loop has
no iteration cap by default (#1226 D1b), so an LM that does not recognize a refusal
as permanent could retry it forever. ``ClioReAct`` classifies each call's exception
(an ``MCPProtocolError``, or a raw ``MCPError`` -32021/-32022 mapped to one) and
re-raises it after the step is recorded. These tests assert the flow terminates on
the typed terminal outcome -- bounded by ASSERTING THE BEHAVIOR (the typed exception
propagates, the LM is invoked once), never by wall-clock timing.
"""

from __future__ import annotations

from typing import Any

import dspy
import pytest
from mcp.shared.exceptions import MCPError

from clio_agent.errors import (
    MCPMissingRequiredClientCapabilityError,
    MCPUnsupportedProtocolVersionError,
)
from clio_agent.gact.agents.clio_react import ClioReAct
from tests._scripted_engine import ScriptedEngine, calls, scripted_lm

pytestmark = pytest.mark.usefixtures("clio_core_plane")


def _make_refusing_tool(calls: list[int]) -> Any:
    def task_echo(payload: str = "") -> str:
        """Refuses: the tasks extension is missing."""
        calls[0] += 1
        raise MCPMissingRequiredClientCapabilityError(
            "task_echo requires the tasks extension",
            {"requiredCapabilities": {"extensions": {"io.modelcontextprotocol/tasks": {}}}},
        )

    return task_echo


def _always_retry_lm(tool: str = "task_echo") -> tuple[dspy.LM, ScriptedEngine]:
    """Scripts the SAME doomed tool call ten times -- an LM that never recognizes a
    deterministic refusal is permanent (the #1275 shape); far more than the ONE call
    the loop must stop at."""
    return scripted_lm([calls((tool, {"payload": "ping"}), text=f"retry {tool}")] * 10)


def test_protocol_refusal_terminates_the_loop_on_the_first_call() -> None:
    """The typed refusal propagates out of ``agent(...)`` and the LM is invoked
    exactly once -- no retry loop."""
    calls = [0]
    agent = ClioReAct(
        "question -> answer", tools=[dspy.Tool(_make_refusing_tool(calls))], max_iters=0
    )
    lm, engine = _always_retry_lm()

    with dspy.context(lm=lm):
        with pytest.raises(MCPMissingRequiredClientCapabilityError) as excinfo:
            agent(question="fetch it")

    assert excinfo.value.reason == "mcp_capability_refused"
    assert calls[0] == 1
    assert len(engine.requests) == 1


def test_protocol_refusal_message_names_the_redial_extension() -> None:
    """D2: the propagated error's message carries what to re-dial with."""
    agent = ClioReAct(
        "question -> answer", tools=[dspy.Tool(_make_refusing_tool([0]))], max_iters=6
    )
    with dspy.context(lm=_always_retry_lm()[0]):
        with pytest.raises(MCPMissingRequiredClientCapabilityError) as excinfo:
            agent(question="fetch it")

    assert "io.modelcontextprotocol/tasks" in str(excinfo.value)


@pytest.mark.parametrize(
    ("code", "typed"),
    [
        (-32021, MCPMissingRequiredClientCapabilityError),
        (-32022, MCPUnsupportedProtocolVersionError),
    ],
)
def test_raw_mcp_refusal_code_is_classified_and_escalated(code: int, typed: type) -> None:
    """A raw SDK ``MCPError`` carrying a refusal code is mapped to the typed clio error
    and escalated -- the SDK class never leaks out of the loop."""

    def task_echo(payload: str = "") -> str:
        """Raw SDK refusal."""
        raise MCPError(code, "refused by server", {"detail": "x"})

    agent = ClioReAct("question -> answer", tools=[dspy.Tool(task_echo)], max_iters=0)
    lm, engine = _always_retry_lm()
    with dspy.context(lm=lm):
        with pytest.raises(typed):
            agent(question="fetch it")
    assert len(engine.requests) == 1


def test_async_tool_refusal_escalates() -> None:
    """An async tool's refusal escalates exactly like a sync one: the classification
    reads the call's own exception, so no context crossing is involved."""
    calls = [0]

    async def task_echo(payload: str = "") -> str:
        """Async refusing tool."""
        calls[0] += 1
        raise MCPMissingRequiredClientCapabilityError(
            "task_echo requires the tasks extension",
            {"requiredCapabilities": {"extensions": {"io.modelcontextprotocol/tasks": {}}}},
        )

    agent = ClioReAct("question -> answer", tools=[dspy.Tool(task_echo)], max_iters=0)
    lm, engine = _always_retry_lm()
    with dspy.context(lm=lm):
        with pytest.raises(MCPMissingRequiredClientCapabilityError):
            agent(question="fetch it")
    assert calls[0] == 1
    assert len(engine.requests) == 1


def test_ordinary_tool_error_is_not_escalated() -> None:
    """An UNTYPED tool error (including a non-refusal ``MCPError`` code) stays the
    model's own retryable observation -- the escalation is scoped strictly to the
    typed refusal class (clio is not the router/decider for ordinary tool failures)."""

    def flaky(payload: str = "") -> str:
        """Transient failure."""
        raise RuntimeError("transient upstream hiccup")

    def rpc_error(payload: str = "") -> str:
        """An MCP error that is not a protocol refusal."""
        raise MCPError(-32603, "internal error")

    agent = ClioReAct(
        "question -> answer", tools=[dspy.Tool(flaky), dspy.Tool(rpc_error)], max_iters=6
    )
    lm, engine = scripted_lm(
        [
            calls(("flaky", {"payload": "x"}), ("rpc_error", {"payload": "x"}), text="try both"),
            calls(("submit", {"answer": "n/a"}), text="give up, submit"),
        ]
    )
    with dspy.context(lm=lm):
        pred = agent(question="fetch it")
    assert (pred.answer, pred.termination_reason) == ("n/a", "submit")
    first, second = pred.messages[3].parts
    assert [first.is_error, second.is_error] == [True, True]
    assert "transient upstream hiccup" in first.content[0].text
    assert len(engine.requests) == 2
