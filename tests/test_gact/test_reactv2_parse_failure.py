"""A step whose model reply cannot be read ends the turn with a typed reason.

Reproduces the shape seen live on Codex Direct: the model announces its next
step in prose ("I need the catalog's map component schema first.") and stops
without making the tool call. The reply parses neither as a tool call nor as
the step's output fields, so the loop cannot continue. Before this fix the loop
discarded the exception and the turn surfaced only ``empty_response`` with no
trace of what the model said; now the prediction carries a recoverable
``model_reply_unparseable`` error holding the raw reply.
"""

from __future__ import annotations

from typing import Any

import dspy
from dspy.utils.dummies import DummyLM

from clio_agent.gact.agents.react_parse_failure import (
    PARSE_FAILURE_ERROR,
    parse_failure_error_info,
)
from clio_agent.gact.agents.reactv2 import retaining_reactv2_cls
from clio_agent.gact.types import ErrorInfo

PROSE_ONLY_REPLY = (
    "I'll use the existing earthquake dataset and the user's magnitude filter, then create "
    "a map-only view of the matching events. I need the catalog's map component schema first."
)


class _ProseOnlyLM(DummyLM):
    """Answers every call with bare prose: no field markers, no tool call."""

    def __init__(self, reply: str) -> None:
        super().__init__([{"answer": reply}] * 4)
        self._reply = reply

    def _format_answer_fields(self, field_names_and_values: dict[str, Any]) -> str:
        return self._reply


def _lookup_schema(component: str = "") -> str:
    return f"schema for {component}"


def test_prose_only_reply_ends_turn_with_typed_error_and_raw_reply() -> None:
    """RED before the fix: ``error_info`` was absent and the raw reply was lost."""

    agent = retaining_reactv2_cls()("question -> answer", tools=[dspy.Tool(_lookup_schema)])

    with dspy.context(lm=_ProseOnlyLM(PROSE_ONLY_REPLY), adapter=dspy.ChatAdapter()):
        pred = agent(question="Okay can you do a map only with these ones?")

    assert pred.termination_reason == "parse_error"
    error_info = pred.error_info
    assert isinstance(error_info, ErrorInfo)
    assert error_info.error == PARSE_FAILURE_ERROR
    assert error_info.recoverable is True
    assert error_info.details["termination_reason"] == "parse_error"
    assert error_info.details["exception"] == "AdapterParseError"
    assert error_info.details["step_index"] == 0
    assert PROSE_ONLY_REPLY in error_info.details["raw_reply"]


def test_parse_failure_error_info_handles_value_error_without_raw_reply() -> None:
    """A plain ``ValueError`` has no ``lm_response``; the reply field is empty, not missing."""

    info = parse_failure_error_info(ValueError("bad tool args"), step_index=3, expert_id="data")

    assert info.error == PARSE_FAILURE_ERROR
    assert info.details["raw_reply"] == ""
    assert info.details["parse_message"] == "bad tool args"
    assert info.details["exception"] == "ValueError"
    assert info.details["step_index"] == 3
    assert info.details["expert_id"] == "data"


def test_successful_turn_carries_no_error_info() -> None:
    """A direct answer leaves ``error_info`` unset so the turn is not marked failed."""

    agent = retaining_reactv2_cls()("question -> answer", tools=[dspy.Tool(_lookup_schema)])
    lm = DummyLM([{"next_thought": "Done.", "tool_calls": {"tool_calls": []}}])

    with dspy.context(lm=lm, adapter=dspy.ChatAdapter()):
        pred = agent(question="hi")

    assert pred.termination_reason == "direct_response"
    assert getattr(pred, "error_info", None) is None
