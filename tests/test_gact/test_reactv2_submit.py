"""S3 pins for the #878 contract rework onto the ReActV2 ``submit`` path (#901 S3).

ReActV2 has no ``extract`` step: the final outputs ride the internal ``submit`` tool's
typed args and flow to the returned ``Prediction`` unchanged. These tests pin that
relocation and its no-silent-fallback records:

* the submit-args VALUE flows to the final Prediction — present (answer +
  workflow_state), absent (a droppable field the model omits), and rejected
  (invalid-typed workflow_state, which must NOT flow);
* each produced final-output field records a ``react_submit_field_suppressed`` reason
  (its value routes to the return contract, not a visible text lane), and a rejected
  submit records ``react_submit_invalid_output`` — the sabotage tripwire is that
  breaking the flow turns the value-flow assertions red.

The classic #878 path (``lm_activity``/``streaming``) is untouched and unreachable
from V2, so its contract tests stay the tripwire for that side.
"""

from __future__ import annotations

from typing import Any

import dspy
import pytest
from dspy.adapters.types.tool import ToolCalls
from dspy.utils.dummies import DummyLM

from clio_agent.gact.agents import reactv2
from clio_agent.gact.agents.reactv2 import _RetainingReActV2, retaining_reactv2_cls


def _search(q: str) -> str:
    return "R"


class _WsSig(dspy.Signature):
    question: str = dspy.InputField()
    answer: str = dspy.OutputField()
    workflow_state: dict[str, Any] = dspy.OutputField()


def _build(signature: Any = "question -> answer") -> _RetainingReActV2:
    cls = retaining_reactv2_cls()
    return cls(signature, tools=[dspy.Tool(_search)], max_iters=4)


def _capture_reasons(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Capture the stream-audit records reactv2 emits (the sink is otherwise a
    configured-only no-op)."""
    records: list[dict[str, Any]] = []

    def _sink(stage: str, **fields: Any) -> None:
        records.append({"stage": stage, **fields})

    monkeypatch.setattr("clio_agent.runtime.stream_audit.stream_audit", _sink)
    return records


def _reasons(records: list[dict[str, Any]]) -> list[str]:
    return [r.get("duplicate_reason") for r in records]


def _submit_call(**args: Any) -> ToolCalls:
    return ToolCalls(tool_calls=[ToolCalls.ToolCall(id="call_0_0", name="submit", args=args)])


# --- 1. present: answer + valid workflow_state flow + are recorded -------------


def test_submit_present_values_flow_and_are_recorded(monkeypatch) -> None:
    records = _capture_reasons(monkeypatch)
    agent = _build(_WsSig)
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

    # VALUE flows to the Prediction (the return contract).
    assert pred.answer == "DONE"
    assert pred.workflow_state == {"status": "complete"}
    # Each produced final-output field is recorded as routed-to-contract (not visible).
    suppressed = [
        r["field"]
        for r in records
        if r.get("duplicate_reason") == reactv2.REACT_SUBMIT_FIELD_SUPPRESSED
    ]
    assert set(suppressed) == {"answer", "workflow_state"}
    assert all(
        r.get("duplicate_suppressed") is True
        for r in records
        if r.get("duplicate_reason") == reactv2.REACT_SUBMIT_FIELD_SUPPRESSED
    )


# --- 2. absent: an omitted required output field is a rejected degraded turn ---


def test_submit_absent_required_field_rejected_and_recorded(monkeypatch) -> None:
    """Stock ReActV2 ``submit`` requires EVERY declared output field (defaults are not
    honored by ``_make_submit_tool``), so a model that omits ``workflow_state`` yields
    an error result — the value does NOT flow and the degraded turn is recorded (never
    silently defaulted). (Droppable-with-default on submit would be a
    ``_make_submit_tool`` override — a later slice; see the report deviation.)"""
    records = _capture_reasons(monkeypatch)
    agent = _build(_WsSig)
    results, final_outputs = agent._execute_tool_calls(_submit_call(answer="ONLY_ANSWER"))
    assert final_outputs is None
    assert results.tool_call_results[0].is_error is True
    assert reactv2.REACT_SUBMIT_INVALID_OUTPUT in _reasons(records)
    assert reactv2.REACT_SUBMIT_FIELD_SUPPRESSED not in _reasons(records)


# --- 3. invalid-typed workflow_state is rejected (value does NOT flow) ---------


def test_execute_tool_calls_records_present_fields() -> None:
    """The S3 chokepoint directly: a valid submit yields final_outputs and the values
    line up — the sabotage tripwire (break the flow -> this goes red)."""
    agent = _build(_WsSig)
    results, final_outputs = agent._execute_tool_calls(
        _submit_call(answer="A", workflow_state={"k": "v"})
    )
    assert final_outputs == {"answer": "A", "workflow_state": {"k": "v"}}
    assert results.tool_call_results[0].is_error is False


def test_invalid_typed_workflow_state_rejected_and_recorded(monkeypatch) -> None:
    records = _capture_reasons(monkeypatch)
    agent = _build(_WsSig)
    # workflow_state typed dict, but the model passes a string -> submit tool rejects.
    results, final_outputs = agent._execute_tool_calls(
        _submit_call(answer="A", workflow_state="NOT_A_DICT")
    )
    # The value did NOT flow (no silent acceptance of a bad-typed output).
    assert final_outputs is None
    assert results.tool_call_results[0].is_error is True
    # The degraded turn is recorded, never silent.
    assert reactv2.REACT_SUBMIT_INVALID_OUTPUT in _reasons(records)
    # And it did NOT masquerade as a successful suppression.
    assert reactv2.REACT_SUBMIT_FIELD_SUPPRESSED not in _reasons(records)


def test_non_submit_calls_are_not_audited(monkeypatch) -> None:
    """A plain tool call (not submit) records no submit reason."""
    records = _capture_reasons(monkeypatch)
    agent = _build(_WsSig)
    agent._execute_tool_calls(
        ToolCalls(tool_calls=[ToolCalls.ToolCall(id="call_0_0", name="search", args={"q": "x"})])
    )
    assert reactv2.REACT_SUBMIT_FIELD_SUPPRESSED not in _reasons(records)
    assert reactv2.REACT_SUBMIT_INVALID_OUTPUT not in _reasons(records)
