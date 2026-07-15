"""S1 pins for clio's ``ReActV2`` subclass — the ONLY expert loop since v0.8.0.

These tests exercise the V2 subclass (:mod:`clio_agent.gact.agents.reactv2`).
The classic ``_RetainingReAct`` and the ``CLIO_REACTV2`` kill-switch were
deleted in the v0.8.0 cleanup. They pin four properties the design calls out:

1. **Append-only composition** (the whole point, design §3): across two scripted
   react steps the structured ``history.messages`` grows append-only (each turn's
   messages are a byte-stable prefix of the next), and the rendered *wire* messages
   share a growing byte-identical prefix — the #891 prompt-cache fingerprint the
   classic single-string trajectory fails.
2. **Reasoning-hijack defense** (design §4, risk 1): the ReAct-internal
   ``next_thought`` output field is typed plain ``str``, NOT ``dspy.Reasoning`` —
   so its native reasoning-channel adaptation cannot hijack clio's thinking lane.
   Sabotage by retyping it ``dspy.Reasoning`` turns this test red.
3. **workflow_state on submit args** (design fact 4): a typed ``workflow_state``
   output field on the user signature rides the internal ``submit`` tool's
   ``arg_types`` unchanged (the load-bearing typed extract survives, relocated).
4. **Single loop (v0.8.0)**: ``_retaining_react_cls`` unconditionally returns
   ``_RetainingReActV2``, and the deleted ``CLIO_REACTV2`` env is inert.
"""

from __future__ import annotations

import copy
from typing import Any

import dspy
import pytest
from dspy.utils.dummies import DummyLM

from clio_agent.arc.prompt_recorder import PromptRecorder
from clio_agent.gact.agents import runtime
from clio_agent.gact.agents.reactv2 import _RetainingReActV2, retaining_reactv2_cls
from clio_agent.providers.claude_code_stateful import classify_delta, is_strict_prefix


def _search(q: str) -> str:
    """A trivial deterministic tool."""
    return "SEARCH_RESULT"


def _two_step_lm() -> DummyLM:
    """Script two ``search`` tool turns then a ``submit`` (ToolCalls/submit shape)."""
    return DummyLM(
        [
            {
                "next_thought": "t0",
                "tool_calls": {"tool_calls": [{"name": "search", "args": {"q": "x"}}]},
            },
            {
                "next_thought": "t1",
                "tool_calls": {"tool_calls": [{"name": "search", "args": {"q": "y"}}]},
            },
            {
                "next_thought": "t2",
                "tool_calls": {"tool_calls": [{"name": "submit", "args": {"answer": "FINAL"}}]},
            },
        ]
    )


def _build_agent(signature: Any = "question -> answer") -> _RetainingReActV2:
    cls = retaining_reactv2_cls()
    return cls(signature, tools=[dspy.Tool(_search)], max_iters=6)


# --- 1. append-only composition ------------------------------------------------


def test_history_messages_grow_append_only_across_steps() -> None:
    """The structured ``history.messages`` handed to each ``self.react`` call is a
    byte-stable prefix of the next call's — append-only by construction (§3)."""
    agent = _build_agent()
    captured: list[list[dict[str, Any]]] = []
    original_react = agent.react

    def spy(**kwargs: Any) -> Any:
        # history is mutated in place (append); snapshot the messages per call.
        captured.append(copy.deepcopy(kwargs["history"].messages))
        return original_react(**kwargs)

    agent.react = spy  # type: ignore[method-assign]
    with dspy.context(lm=_two_step_lm(), adapter=dspy.ChatAdapter()):
        pred = agent(question="find alpha")

    assert pred.answer == "FINAL"
    assert pred.termination_reason == "submit"
    # turn 0 sees an empty history, then one committed event per prior turn.
    assert [len(m) for m in captured] == [0, 1, 2]
    for earlier, later in zip(captured, captured[1:], strict=False):
        assert later[: len(earlier)] == earlier, "history is not an append-only prefix"
        assert len(later) > len(earlier), "history did not grow"


def test_wire_messages_share_a_growing_byte_prefix() -> None:
    """The #891 fingerprint, asserted on the FULL wire message list via the real delta
    detector (#901): consecutive ``self.react`` calls are an append-only extension of
    each other beneath a single byte-static trailing block (the ChatAdapter
    ``main_request`` closing instruction / current-input block, which never changes bytes
    but moves position). The classic single-string trajectory fails this; V2's
    append-only history passes it — ``classify_delta`` returns a real ``delta`` (not a
    ``prefix_mismatch``) over the whole list, which is exactly what engages the Claude
    stateful session-delta transport."""
    agent = _build_agent()
    recorder = PromptRecorder()
    with dspy.context(lm=_two_step_lm(), adapter=dspy.ChatAdapter(), callbacks=[recorder]):
        agent(question="find alpha")

    calls = recorder.calls()
    assert len(calls) == 3  # two tool turns + the submit turn

    # The FULL message list of call N is a delta-extension of call N-1's FULL list under
    # the two-rung structural contract (pure strict prefix, else strict prefix beneath a
    # byte-identical static tail). No slicing off the tail by hand — the detector owns it.
    for earlier_call, later_call in zip(calls[1:], calls[2:], strict=False):
        plan = classify_delta(earlier_call.messages, later_call.messages)
        assert plan.mode == "delta", f"expected a delta, got {plan.mode}/{plan.reason}"
        assert plan.reason is None
        assert len(plan.messages) >= 1, "the delta must carry the newly-appended messages"
        assert len(later_call.messages) > len(earlier_call.messages)

    # The static trailing block is byte-identical across the delta calls (the tail the
    # contract tolerates), so a plain strict prefix is NOT what holds here — the extended
    # contract is load-bearing.
    assert calls[1].messages[-1] == calls[2].messages[-1]
    assert not is_strict_prefix(calls[1].messages, calls[2].messages)


# --- 2. Reasoning-hijack defense (sabotage pin) --------------------------------


def test_next_thought_is_plain_str_not_reasoning() -> None:
    """``next_thought`` is typed plain ``str`` and ``dspy.Reasoning`` appears on NO
    output field — the frozen-contract defense against native reasoning-lane hijack
    (§4, risk 1). Retyping the field ``dspy.Reasoning`` turns this red."""
    agent = _build_agent()
    react_signature = agent.react.signature
    next_thought = react_signature.output_fields["next_thought"]

    assert next_thought.annotation is str
    assert not any(
        field.annotation is dspy.Reasoning for field in react_signature.output_fields.values()
    ), "a dspy.Reasoning output field would hijack the provider CoT / thinking lane"


# --- 3. workflow_state rides the submit tool's typed args ----------------------


def test_workflow_state_rides_submit_tool_arg_types() -> None:
    """A typed ``workflow_state`` output field on the user signature becomes a typed
    arg of the internal ``submit`` tool (design fact 4) — the load-bearing typed
    extract survives on V2, relocated onto submit rather than a ChainOfThought."""

    class _Sig(dspy.Signature):
        question: str = dspy.InputField()
        answer: str = dspy.OutputField()
        workflow_state: dict[str, Any] = dspy.OutputField()

    agent = _build_agent(_Sig)
    submit = agent.tools["submit"]
    assert "workflow_state" in submit.arg_types
    assert submit.arg_types["workflow_state"] == dict[str, Any]
    assert submit.arg_types["answer"] is str


def test_submit_returns_typed_workflow_state_value() -> None:
    """End-to-end: the model's ``submit`` call carries the typed ``workflow_state``
    value through to the final Prediction."""

    class _Sig(dspy.Signature):
        question: str = dspy.InputField()
        answer: str = dspy.OutputField()
        workflow_state: dict[str, Any] = dspy.OutputField()

    agent = _build_agent(_Sig)
    lm = DummyLM(
        [
            {
                "next_thought": "submit now",
                "tool_calls": {
                    "tool_calls": [
                        {
                            "name": "submit",
                            "args": {"answer": "DONE", "workflow_state": {"status": "complete"}},
                        }
                    ]
                },
            }
        ]
    )
    with dspy.context(lm=lm, adapter=dspy.ChatAdapter()):
        pred = agent(question="q")
    assert pred.answer == "DONE"
    assert pred.workflow_state == {"status": "complete"}


# --- 4. single loop (v0.8.0) ----------------------------------------------------


def test_production_class_is_v2() -> None:
    """``_retaining_react_cls`` unconditionally selects ``_RetainingReActV2`` —
    the classic ``_RetainingReAct`` was deleted in the v0.8.0 cleanup."""
    cls = runtime._retaining_react_cls()
    assert cls is _RetainingReActV2
    assert issubclass(cls, dspy.ReActV2)
    assert not hasattr(runtime, "_reactv2_enabled")


def test_deleted_kill_switch_env_is_inert(monkeypatch: pytest.MonkeyPatch) -> None:
    """SABOTAGE twin: CLIO_REACTV2=0 (the deleted #901 kill-switch) must not
    resurrect the classic loop — V2 is selected regardless."""
    from clio_agent import conf

    monkeypatch.setenv("CLIO_REACTV2", "0")
    conf.reload()
    try:
        assert runtime._retaining_react_cls() is _RetainingReActV2
    finally:
        monkeypatch.delenv("CLIO_REACTV2", raising=False)
        conf.reload()
