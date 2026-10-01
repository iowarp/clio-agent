"""ClioReAct -- clio's own ReAct loop (agent-loop rebuild, phase 2).

Pins the loop contract against a scripted LM (no network):

* the differential against stock ``dspy.ReActV2`` -- same step messages, same final
  outputs; clio's wire is append-only across calls, stock's is not;
* a step's tool calls run concurrently and keep call order; one failing call does
  not stop the others;
* a terminal MCP protocol refusal escalates out of the loop (#1275), async tools run;
* cancellation is checked at every step boundary;
* termination: ``direct_response`` / ``submit`` (declared defaults honored) /
  ``max_iters`` (``<= 0`` unlimited) / ``parse_error``; nothing calls the model after
  the loop.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from typing import Any

import dspy
import pytest
from dspy.utils.dummies import DummyLM

from clio_agent.agent import cancellation_checker
from clio_agent.errors import MCPMissingRequiredClientCapabilityError
from clio_agent.gact.agents.clio_react import ClioReAct
from clio_agent.gact.runtime.globals import _TurnCancelled
from clio_agent.lm.adapters import _lenient_chat_adapter_cls


def _calls(*calls: tuple[str, dict[str, Any]]) -> str:
    return json.dumps({"tool_calls": [{"name": n, "args": a} for n, a in calls]})


def _step(thought: str, *calls: tuple[str, dict[str, Any]]) -> dict[str, str]:
    return {"next_thought": thought, "tool_calls": _calls(*calls)}


def search(query: str) -> str:
    """Search the corpus."""
    return f"results for {query}"


def _run(
    steps: list[dict[str, str]],
    tools: list[Any],
    *,
    signature: Any = "question -> answer",
    max_iters: int = 8,
    module_cls: Any = ClioReAct,
    adapter: Any = None,
) -> tuple[dspy.Prediction, DummyLM]:
    adapter = adapter or _lenient_chat_adapter_cls()()
    lm = DummyLM([dict(s) for s in steps], adapter=adapter)
    with dspy.context(lm=lm, adapter=adapter):
        pred = module_cls(signature, tools=tools, max_iters=max_iters)(question="what?")
    return pred, lm


def _wires(lm: DummyLM) -> list[list[dict[str, Any]]]:
    return [entry["messages"] for entry in lm.history]


# --------------------------------------------------------------------------- #
# differential vs stock ReActV2                                               #
# --------------------------------------------------------------------------- #
DIFF_STEPS = [
    _step("look it up", ("search", {"query": "x"})),
    _step("look again", ("search", {"query": "y"})),
    _step("done", ("submit", {"answer": "42"})),
]


def test_differential_same_steps_and_outputs_as_stock_reactv2() -> None:
    clio, clio_lm = _run(DIFF_STEPS, [search])
    stock, stock_lm = _run(
        DIFF_STEPS, [search], module_cls=dspy.ReActV2, adapter=dspy.ChatAdapter()
    )

    assert (clio.termination_reason, clio.answer) == (stock.termination_reason, stock.answer)
    assert (clio.answer, clio.termination_reason) == ("42", "submit")
    assert len(clio_lm.history) == len(stock_lm.history) == 3

    def step_messages(wire: list[dict[str, Any]]) -> list[dict[str, Any]]:
        # assistant steps + tool results: everything that is not the system prompt,
        # the task head, or the closing "Respond with ..." instruction.
        return [
            m for m in wire if m["role"] == "assistant" or "tool_call_results" in str(m["content"])
        ]

    for clio_wire, stock_wire in zip(_wires(clio_lm), _wires(stock_lm), strict=True):
        assert step_messages(clio_wire) == step_messages(stock_wire)


def test_wire_is_append_only_unlike_stock() -> None:
    _, clio_lm = _run(DIFF_STEPS, [search])
    _, stock_lm = _run(DIFF_STEPS, [search], module_cls=dspy.ReActV2, adapter=dspy.ChatAdapter())

    def append_only(wires: list[list[dict[str, Any]]]) -> bool:
        return all(
            prev[:-1] == cur[: len(prev) - 1] and prev[-1] == cur[-1]
            for prev, cur in zip(wires, wires[1:], strict=False)
        )

    assert append_only(_wires(clio_lm))
    # stock moves the tools from the first user message to a trailing one after call 0
    assert not append_only(_wires(stock_lm))


# --------------------------------------------------------------------------- #
# termination                                                                 #
# --------------------------------------------------------------------------- #
def test_no_tool_call_is_the_answer_in_one_call() -> None:
    pred, lm = _run([_step("Ready.")], [search])

    assert (pred.answer, pred.termination_reason) == ("Ready.", "direct_response")
    assert len(lm.history) == 1


def test_parse_error_stops_without_another_model_call() -> None:
    # The loop itself never re-calls the model on a parse failure; DSPy's own
    # JSON-adapter retry is an adapter setting, off here.
    adapter = _lenient_chat_adapter_cls()(use_json_adapter_fallback=False)
    pred, lm = _run([{"next_thought": "x", "tool_calls": "not json"}], [search], adapter=adapter)

    assert pred.termination_reason == "parse_error"
    assert len(lm.history) == 1


def test_max_iters_cap_stops_and_zero_is_unlimited() -> None:
    steps = [_step(f"s{i}", ("search", {"query": str(i)})) for i in range(3)]
    capped, lm = _run(steps, [search], max_iters=2)
    assert capped.termination_reason == "max_iters"
    assert len(lm.history) == 2

    unlimited, lm = _run([*steps, _step("done")], [search], max_iters=0)
    assert (unlimited.termination_reason, unlimited.answer) == ("direct_response", "done")
    assert len(lm.history) == 4


def test_submit_fills_a_declared_default_and_requires_the_rest() -> None:
    class Sig(dspy.Signature):
        question: str = dspy.InputField()
        answer: str = dspy.OutputField()
        notes: str = dspy.OutputField(default="none")

    pred, _ = _run([_step("done", ("submit", {"answer": "a"}))], [search], signature=Sig)
    assert (pred.answer, pred.notes, pred.termination_reason) == ("a", "none", "submit")

    pred, lm = _run(
        [_step("done", ("submit", {"notes": "n"})), _step("fixed", ("submit", {"answer": "b"}))],
        [search],
        signature=Sig,
    )
    assert (pred.answer, pred.termination_reason) == ("b", "submit")
    assert "Missing required final output field(s): answer" in str(lm.history[1]["messages"])


def test_submit_is_reserved() -> None:
    def submit(answer: str) -> str:
        return answer

    with pytest.raises(ValueError, match="reserved"):
        ClioReAct("question -> answer", tools=[submit])


# --------------------------------------------------------------------------- #
# concurrent tools                                                            #
# --------------------------------------------------------------------------- #
def test_a_steps_calls_run_concurrently_in_call_order() -> None:
    barrier = threading.Barrier(2, timeout=5)

    def slow(tag: str) -> str:
        """Slow tool."""
        barrier.wait()  # both calls must be in flight at once, or this times out
        time.sleep(0.3)
        return f"done {tag}"

    started = time.monotonic()
    pred, lm = _run(
        [_step("both", ("slow", {"tag": "a"}), ("slow", {"tag": "b"})), _step("ok")],
        [slow],
    )
    elapsed = time.monotonic() - started

    assert pred.termination_reason == "direct_response"
    assert elapsed < 0.55  # serial would be >= 0.6s
    results = json.loads(str(lm.history[1]["messages"][-2]["content"]).split("\n", 1)[1])
    assert [r["value"] for r in results["tool_call_results"]] == ["done a", "done b"]


def test_one_failing_call_does_not_stop_the_other() -> None:
    def boom(x: str) -> str:
        """Fails."""
        raise RuntimeError("nope")

    pred, lm = _run(
        [_step("both", ("boom", {"x": "1"}), ("search", {"query": "q"})), _step("ok")],
        [boom, search],
    )
    results = json.loads(str(lm.history[1]["messages"][-2]["content"]).split("\n", 1)[1])
    first, second = results["tool_call_results"]
    assert first["is_error"] and "Execution error in boom" in first["value"]
    assert (second["is_error"], second["value"]) == (False, "results for q")
    assert pred.termination_reason == "direct_response"


def test_unknown_tool_is_an_error_observation() -> None:
    _, lm = _run([_step("x", ("nope", {})), _step("ok")], [search])
    assert "Unknown tool: nope" in str(lm.history[1]["messages"])


def test_async_tool_runs() -> None:
    async def fetch(url: str) -> str:
        """Async fetch."""
        await asyncio.sleep(0)
        return f"got {url}"

    _, lm = _run([_step("x", ("fetch", {"url": "u"})), _step("ok")], [fetch])
    assert "got u" in str(lm.history[1]["messages"])


def test_terminal_protocol_refusal_escalates_out_of_the_loop() -> None:
    def refused(x: str) -> str:
        """Refuses."""
        raise MCPMissingRequiredClientCapabilityError("needs capability", {})

    with pytest.raises(MCPMissingRequiredClientCapabilityError):
        _run(
            [_step("x", ("refused", {"x": "1"}), ("search", {"query": "q"})), _step("never")],
            [refused, search],
        )


# --------------------------------------------------------------------------- #
# cancellation                                                                #
# --------------------------------------------------------------------------- #
def test_cancel_before_the_first_call() -> None:
    with cancellation_checker(lambda: True), pytest.raises(_TurnCancelled):
        _run([_step("never")], [search])


def test_cancel_during_a_step_stops_before_the_next_call() -> None:
    cancelled = threading.Event()

    def cancel_me(x: str) -> str:
        """Requests cancellation."""
        cancelled.set()
        return "ok"

    adapter = _lenient_chat_adapter_cls()()
    lm = DummyLM([_step("x", ("cancel_me", {"x": "1"})), _step("never")], adapter=adapter)
    with (
        cancellation_checker(cancelled.is_set),
        dspy.context(lm=lm, adapter=adapter),
        pytest.raises(_TurnCancelled),
    ):
        ClioReAct("question -> answer", tools=[cancel_me])(question="q")
    assert len(lm.history) == 1
