"""Identical tool calls in one model reply run once, and the model is told."""

from __future__ import annotations

import dspy
import pytest

from clio_agent.gact.agents.clio_react import ClioReAct
from clio_agent.gact.agents.tool_call_dedupe import collapse_identical
from tests._scripted_engine import calls, scripted_lm

pytestmark = pytest.mark.usefixtures("clio_core_plane")

RAN: list[str] = []


def _search(q: str) -> str:
    """A deterministic search tool that counts its runs."""
    RAN.append(q)
    return f"RESULT {q}"


def test_561_identical_calls_run_once_and_distinct_calls_all_run() -> None:
    RAN.clear()
    agent = ClioReAct("question -> answer", tools=[dspy.Tool(_search, name="search")], max_iters=3)
    repeated = [("search", {"q": "same"})] * 561 + [("search", {"q": "other"})]
    lm, _engine = scripted_lm([calls(*repeated), calls(("submit", {"answer": "done"}))])
    with dspy.context(lm=lm):
        pred = agent(question="q")

    assert RAN == ["same", "other"]
    tool_message = pred.messages[2]
    assert tool_message.role == "tool"
    assert len(tool_message.parts) == 2  # one result per distinct call
    first = "".join(getattr(p, "text", "") for p in tool_message.parts[0].content)
    assert "RESULT same" in first and "repeated this exact search call 560 more times" in first
    second = "".join(getattr(p, "text", "") for p in tool_message.parts[1].content)
    assert "repeated" not in second


def test_collapse_keys_on_name_and_arguments_and_keeps_order() -> None:
    class Call:
        def __init__(self, id_: str, name: str, input_: dict) -> None:
            self.id, self.name, self.input = id_, name, input_

    reply = [
        Call("a", "x", {"k": 1, "j": 2}),
        Call("b", "x", {"j": 2, "k": 1}),  # same arguments, other key order
        Call("c", "y", {"k": 1}),
        Call("d", "x", {"k": 2}),
    ]
    kept, repeats = collapse_identical(reply)
    assert [c.id for c in kept] == ["a", "c", "d"]
    assert repeats == {"a": 1}
