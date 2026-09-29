"""ClioReAct -- clio's agent loop on DSPy 3.4's direct LM interface.

Pins the loop contract against a scripted lm15 engine (no network):

* the semantic differential against stock ``dspy.ReActV2`` (native tools, DSPy 3.4)
  -- the same tool calls, the same results, the same final outputs;
* one ``Request`` per step: system prompt, the task head, then every earlier step as
  typed assistant / tool messages; append-only across calls;
* a step's tool calls run concurrently and keep call order; one failing call does not
  stop the others; an unreadable text-protocol block is an error observation;
* a terminal MCP protocol refusal escalates out of the loop (#1275); async tools run;
* cancellation is checked at every step boundary;
* termination: ``direct_response`` / ``submit`` (declared defaults honored) /
  ``max_iters`` (``<= 0`` unlimited) / ``context_window_exceeded``; nothing calls the
  model after the loop.
"""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Any

import dspy
import pytest
from dspy.lm15 import ContextLengthError, Request, ToolCallPart, ToolResultPart

from clio_agent.agent import cancellation_checker
from clio_agent.errors import MCPMissingRequiredClientCapabilityError
from clio_agent.gact.agents.clio_react import ClioReAct, NoLanguageModelError
from clio_agent.gact.runtime.globals import _TurnCancelled
from clio_agent.lm.engines.text_tools import INVALID_TOOL_CALL
from tests._scripted_engine import Reply, ScriptedEngine, calls, scripted_lm, wire


def search(query: str) -> str:
    """Search the corpus."""
    return f"results for {query}"


def _run(
    script: list[Reply],
    tools: list[Any],
    *,
    signature: Any = "question -> answer",
    max_iters: int = 8,
    module_cls: Any = ClioReAct,
    adapter: Any = None,
    **lm_kwargs: Any,
) -> tuple[dspy.Prediction, ScriptedEngine]:
    lm, engine = scripted_lm(script, **lm_kwargs)
    with dspy.context(lm=lm, adapter=adapter):
        pred = module_cls(signature, tools=tools, max_iters=max_iters)(question="what?")
    return pred, engine


def _tool_steps(request: Request) -> list[tuple[str, Any]]:
    """The request's calls and results (name/args and value/error), ids dropped."""
    out: list[tuple[str, Any]] = []
    for message in request.messages:
        for part in message.parts:
            if isinstance(part, ToolCallPart):
                out.append(("call", (part.name, dict(part.input))))
            elif isinstance(part, ToolResultPart):
                text = "".join(getattr(p, "text", "") for p in part.content)
                out.append(("result", (text, part.is_error)))
    return out


# --------------------------------------------------------------------------- #
# differential vs stock ReActV2 (native tools)                                #
# --------------------------------------------------------------------------- #
DIFF_STEPS = [
    calls(("search", {"query": "x"}), text="look it up"),
    calls(("search", {"query": "y"}), text="look again"),
    calls(("submit", {"answer": "42"}), text="done"),
]


def test_differential_same_calls_results_and_outputs_as_stock_reactv2() -> None:
    clio, clio_engine = _run(DIFF_STEPS, [search])
    stock, stock_engine = _run(
        DIFF_STEPS,
        [search],
        module_cls=dspy.ReActV2,
        adapter=dspy.ChatAdapter(use_native_function_calling=True),
    )

    assert (clio.answer, clio.termination_reason) == ("42", "submit")
    assert (stock.answer, stock.termination_reason) == ("42", "submit")
    assert len(clio_engine.requests) == len(stock_engine.requests) == 3
    for mine, theirs in zip(clio_engine.requests, stock_engine.requests, strict=True):
        assert _tool_steps(mine) == _tool_steps(theirs)
        assert sorted(t.name for t in mine.tools) == sorted(t.name for t in theirs.tools)


def test_each_step_is_one_request_and_the_wire_is_append_only() -> None:
    _, engine = _run(DIFF_STEPS, [search])

    wires = [wire(r) for r in engine.requests]
    assert wires[0] == [("user", [("text", "what?")])]
    assert wires[1][1] == (
        "assistant",
        [("text", "look it up"), ("call", "call_0_0", "search", {"query": "x"})],
    )
    assert wires[1][2] == ("tool", [("result", "call_0_0", "results for x", False)])
    for prev, cur in zip(wires, wires[1:], strict=False):
        assert cur[: len(prev)] == prev
    assert {r.system for r in engine.requests} == {engine.requests[0].system}


def test_thinking_goes_back_with_its_step() -> None:
    _, engine = _run(
        [calls(("search", {"query": "x"}), thinking="plan: search"), Reply(text="ok")], [search]
    )
    assert wire(engine.requests[1])[1] == (
        "assistant",
        [("thinking", "plan: search"), ("call", "call_0_0", "search", {"query": "x"})],
    )


def test_the_expert_system_prompt_is_the_system_message() -> None:
    lm, engine = scripted_lm([Reply(text="hi")])
    with dspy.context(lm=lm):
        ClioReAct("system_prompt, question -> answer", tools=[search])(
            system_prompt="You are the data expert.", question="what?"
        )
    request = engine.requests[0]
    assert request.system == "You are the data expert."
    assert wire(request) == [("user", [("text", "what?")])]


def test_generation_settings_ride_the_request_config() -> None:
    _, engine = _run([Reply(text="ok")], [search], temperature=0.2, max_tokens=900)
    config = engine.requests[0].config
    assert (config.temperature, config.max_tokens) == (0.2, 900)


def test_no_lm_bound_is_a_typed_error() -> None:
    with dspy.context(lm=None), pytest.raises(NoLanguageModelError):
        ClioReAct("question -> answer", tools=[search])(question="q")


# --------------------------------------------------------------------------- #
# termination                                                                 #
# --------------------------------------------------------------------------- #
def test_no_tool_call_is_the_answer_in_one_call() -> None:
    pred, engine = _run([Reply(text="Ready.")], [search])

    assert (pred.answer, pred.termination_reason) == ("Ready.", "direct_response")
    assert len(engine.requests) == 1


def test_context_window_exceeded_stops_without_another_call() -> None:
    pred, engine = _run([Reply(raises=ContextLengthError("too long"))], [search])
    assert pred.termination_reason == "context_window_exceeded"
    assert len(engine.requests) == 1


def test_an_engines_own_typed_error_reaches_the_turn_as_itself() -> None:
    from clio_agent.providers.claude_code_plan_limit import ClaudeCodePlanLimitError

    limit = ClaudeCodePlanLimitError("Claude subscription usage limit reached")
    with pytest.raises(ClaudeCodePlanLimitError) as raised:
        _run([Reply(raises=limit)], [search])
    assert raised.value is limit


def test_a_truncated_reply_is_a_typed_turn_failure() -> None:
    from clio_agent.lm.policy import LMOutputTruncatedError

    with pytest.raises(LMOutputTruncatedError):
        _run([Reply(text="The answer is", finish_reason="length")], [search])


def test_max_iters_cap_stops_and_zero_is_unlimited() -> None:
    steps = [calls(("search", {"query": str(i)})) for i in range(3)]
    capped, engine = _run(steps, [search], max_iters=2)
    assert capped.termination_reason == "max_iters"
    assert len(engine.requests) == 2

    unlimited, engine = _run([*steps, Reply(text="done")], [search], max_iters=0)
    assert (unlimited.termination_reason, unlimited.answer) == ("direct_response", "done")
    assert len(engine.requests) == 4


def test_submit_fills_a_declared_default_and_requires_the_rest() -> None:
    class Sig(dspy.Signature):
        question: str = dspy.InputField()
        answer: str = dspy.OutputField()
        notes: str = dspy.OutputField(default="none")

    pred, _ = _run([calls(("submit", {"answer": "a"}))], [search], signature=Sig)
    assert (pred.answer, pred.notes, pred.termination_reason) == ("a", "none", "submit")

    pred, engine = _run(
        [calls(("submit", {"notes": "n"})), calls(("submit", {"answer": "b"}))],
        [search],
        signature=Sig,
    )
    assert (pred.answer, pred.termination_reason) == ("b", "submit")
    assert "Missing required final output field(s): answer" in str(wire(engine.requests[1]))


def test_submit_is_reserved() -> None:
    def submit(answer: str) -> str:
        return answer

    with pytest.raises(ValueError, match="reserved"):
        ClioReAct("question -> answer", tools=[submit])


# --------------------------------------------------------------------------- #
# tools                                                                       #
# --------------------------------------------------------------------------- #
def _results(request: Request) -> list[tuple[str, bool]]:
    return [(text, err) for kind, (text, err) in _tool_steps(request) if kind == "result"]


def test_a_steps_calls_run_concurrently_in_call_order() -> None:
    barrier = threading.Barrier(2, timeout=5)

    def slow(tag: str) -> str:
        """Slow tool."""
        barrier.wait()  # both calls must be in flight at once, or this times out
        time.sleep(0.05)
        return f"done {tag}"

    pred, engine = _run(
        [calls(("slow", {"tag": "a"}), ("slow", {"tag": "b"})), Reply(text="ok")], [slow]
    )

    assert pred.termination_reason == "direct_response"
    assert _results(engine.requests[1]) == [("done a", False), ("done b", False)]


def test_one_failing_call_does_not_stop_the_other() -> None:
    def boom(x: str) -> str:
        """Fails."""
        raise RuntimeError("nope")

    pred, engine = _run(
        [calls(("boom", {"x": "1"}), ("search", {"query": "q"})), Reply(text="ok")],
        [boom, search],
    )
    (first, first_err), second = _results(engine.requests[1])
    assert first_err and "Execution error in boom" in first
    assert second == ("results for q", False)
    assert pred.termination_reason == "direct_response"


def test_unknown_tool_is_an_error_observation() -> None:
    _, engine = _run([calls(("nope", {})), Reply(text="ok")], [search])
    assert _results(engine.requests[1]) == [("Unknown tool: nope", True)]


def test_an_unreadable_text_protocol_block_is_an_error_observation() -> None:
    bad = calls((INVALID_TOOL_CALL, {"error": "not JSON", "block": "{oops"}))
    _, engine = _run([bad, Reply(text="ok")], [search])
    [(text, is_error)] = _results(engine.requests[1])
    assert is_error and "not JSON" in text


def test_async_tool_runs() -> None:
    async def fetch(url: str) -> str:
        """Async fetch."""
        await asyncio.sleep(0)
        return f"got {url}"

    _, engine = _run([calls(("fetch", {"url": "u"})), Reply(text="ok")], [fetch])
    assert _results(engine.requests[1]) == [("got u", False)]


def test_terminal_protocol_refusal_escalates_out_of_the_loop() -> None:
    def refused(x: str) -> str:
        """Refuses."""
        raise MCPMissingRequiredClientCapabilityError("needs capability", {})

    with pytest.raises(MCPMissingRequiredClientCapabilityError):
        _run(
            [calls(("refused", {"x": "1"}), ("search", {"query": "q"})), Reply(text="never")],
            [refused, search],
        )


# --------------------------------------------------------------------------- #
# cancellation                                                                #
# --------------------------------------------------------------------------- #
def test_cancel_before_the_first_call() -> None:
    lm, engine = scripted_lm([Reply(text="never")])
    with (
        cancellation_checker(lambda: True),
        dspy.context(lm=lm),
        pytest.raises(_TurnCancelled),
    ):
        ClioReAct("question -> answer", tools=[search])(question="q")
    assert engine.requests == []


def test_cancel_during_a_step_stops_before_the_next_call() -> None:
    cancelled = threading.Event()

    def cancel_me(x: str) -> str:
        """Requests cancellation."""
        cancelled.set()
        return "ok"

    lm, engine = scripted_lm([calls(("cancel_me", {"x": "1"})), Reply(text="never")])
    with (
        cancellation_checker(cancelled.is_set),
        dspy.context(lm=lm),
        pytest.raises(_TurnCancelled),
    ):
        ClioReAct("question -> answer", tools=[cancel_me])(question="q")
    assert len(engine.requests) == 1


# --------------------------------------------------------------------------- #
# tool-result media placement                                                 #
# --------------------------------------------------------------------------- #
def _media_step() -> tuple[list[Any], Any]:
    from dspy.lm15 import ImagePart, Message, TextPart

    image = ImagePart(data="aGk=", media_type="image/png")
    result = ToolResultPart(id="c1", name="view_image", content=(TextPart(text="img"), image))
    return [Message(role="tool", parts=(result,))], image


def test_native_placement_keeps_media_in_the_tool_result() -> None:
    from clio_agent.gact.agents.clio_react import _place_tool_media

    messages, _ = _media_step()
    assert _place_tool_media(messages, "native") == messages


def test_text_only_tool_rows_get_the_media_in_the_next_user_message() -> None:
    from clio_agent.gact.agents.clio_react import _place_tool_media

    messages, image = _media_step()
    tool, user = _place_tool_media(messages, "user_message")
    assert wire(Request(model="m", messages=(tool, user)))[0] == (
        "tool",
        [
            (
                "result",
                "c1",
                "img(1 attachment(s) of this result follow in the next message)",
                False,
            )
        ],
    )
    assert user.role == "user"
    assert user.parts[1:] == (image,)


def test_the_loop_places_media_by_the_lms_tag(monkeypatch: pytest.MonkeyPatch) -> None:
    from dspy.lm15 import ImagePart

    def view(path: str) -> Any:
        """View."""
        return "x"

    lm, engine = scripted_lm([calls(("view", {"path": "p"})), Reply(text="ok")])
    lm._clio_tool_result_media = "user_message"  # type: ignore[attr-defined]
    image = ImagePart(data="aGk=", media_type="image/png")
    from clio_agent.gact.agents import clio_react_record

    original = clio_react_record.result_part

    def with_image(call_id: str, name: str, value: Any, is_error: bool) -> Any:
        part = original(call_id, name, value, is_error)
        return ToolResultPart(id=part.id, name=part.name, content=(*part.content, image))

    monkeypatch.setattr(clio_react_record, "result_part", with_image)
    with dspy.context(lm=lm):
        ClioReAct("question -> answer", tools=[view])(question="q")
    roles = [m.role for m in engine.requests[1].messages]
    assert roles == ["user", "assistant", "tool", "user"]
    assert engine.requests[1].messages[-1].parts[-1] == image


# --------------------------------------------------------------------------- #
# the persistent LM loop                                                      #
# --------------------------------------------------------------------------- #
def test_every_step_runs_on_one_persistent_loop_in_the_callers_context() -> None:
    from clio_agent.gact import context as gact_context

    seen: list[tuple[int, str]] = []

    class Recording(ScriptedEngine):
        def _next(self, request: Request) -> Any:
            seen.append((id(asyncio.get_running_loop()), gact_context.active_session_id()))
            return super()._next(request)

    from tests._scripted_engine import AsyncScriptedEngine

    engine = Recording([calls(("search", {"query": "x"})), Reply(text="ok")])
    lm = dspy.LM(
        "scripted/model",
        engine=engine,
        async_engine=AsyncScriptedEngine(engine),
        cache=False,
        num_retries=0,
    )
    token = gact_context.set_session_id("sess-loop")
    try:
        with dspy.context(lm=lm):
            ClioReAct("question -> answer", tools=[search])(question="q")
    finally:
        gact_context.reset(token)
    loops = {loop for loop, _ in seen}
    assert len(seen) == 2 and len(loops) == 1
    assert {session for _, session in seen} == {"sess-loop"}
