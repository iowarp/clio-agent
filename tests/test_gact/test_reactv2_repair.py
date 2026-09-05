"""Submit-schema and History-retention contracts for CLIO's ReActV2 owner."""

from __future__ import annotations

import inspect
from typing import Any

import dspy
import pytest
from dspy.adapters.types.tool import ToolCalls
from dspy.utils.dummies import DummyLM

from clio_agent.gact import context as ctx
from clio_agent.gact.agents import reactv2, reactv2_events
from clio_agent.gact.agents.reactv2 import _RetainingReActV2, retaining_reactv2_cls


def _search(q: str) -> str:
    return "SEARCH_RESULT"


class _WsSig(dspy.Signature):
    """Two required outputs; omitting either is a real submit rejection."""

    question: str = dspy.InputField()
    answer: str = dspy.OutputField()
    workflow_state: dict[str, Any] = dspy.OutputField()


class _DefaultedSig(dspy.Signature):
    """The declared workflow-state default may satisfy an omitted submit arg."""

    question: str = dspy.InputField()
    answer: str = dspy.OutputField()
    workflow_state: dict[str, Any] = dspy.OutputField(default_factory=dict)


def _build(signature: Any = "question -> answer", max_iters: int = 4) -> _RetainingReActV2:
    return retaining_reactv2_cls()(
        signature,
        tools=[dspy.Tool(_search)],
        max_iters=max_iters,
    )


def _capture_reasons(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []

    def sink(stage: str, **fields: Any) -> None:
        records.append({"stage": stage, **fields})

    monkeypatch.setattr("clio_agent.runtime.stream_audit.stream_audit", sink)
    return records


def _reasons(records: list[dict[str, Any]]) -> list[str]:
    return [str(record.get("duplicate_reason") or "") for record in records]


def _submit_call(**args: Any) -> ToolCalls:
    return ToolCalls(tool_calls=[ToolCalls.ToolCall(id="call_0_0", name="submit", args=args)])


def test_declared_default_field_is_droppable_and_flows_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An author-declared default is schema intent, not runtime fabrication."""
    records = _capture_reasons(monkeypatch)
    agent = _build(_DefaultedSig)

    results, final_outputs = agent._execute_tool_calls(_submit_call(answer="ONLY_ANSWER"))

    assert final_outputs == {"answer": "ONLY_ANSWER", "workflow_state": {}}
    assert results.tool_call_results[0].is_error is False
    assert reactv2.REACT_SUBMIT_INVALID_OUTPUT not in _reasons(records)
    assert reactv2.REACT_SUBMIT_FIELD_SUPPRESSED in _reasons(records)


def test_required_field_without_default_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Missing required structured output is surfaced by the submit tool."""
    records = _capture_reasons(monkeypatch)
    agent = _build(_WsSig)

    results, final_outputs = agent._execute_tool_calls(_submit_call(answer="ONLY_ANSWER"))

    assert final_outputs is None
    assert results.tool_call_results[0].is_error is True
    assert reactv2.REACT_SUBMIT_INVALID_OUTPUT in _reasons(records)


def test_default_field_arg_schema_matches_declared_outputs() -> None:
    """The tool schema still advertises every structured output to the model."""
    submit = _build(_DefaultedSig).tools["submit"]

    assert set(submit.arg_types) == {"answer", "workflow_state"}
    assert submit.arg_types["workflow_state"] == dict[str, Any]


def test_droppable_default_flows_through_an_in_loop_submit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A valid model-selected submit remains the structured completion path."""
    _capture_reasons(monkeypatch)
    agent = _build(_DefaultedSig)
    lm = DummyLM(
        [
            {
                "next_thought": "submit now",
                "tool_calls": {"tool_calls": [{"name": "submit", "args": {"answer": "DONE"}}]},
            }
        ]
    )

    with dspy.context(lm=lm, adapter=dspy.ChatAdapter()):
        prediction = agent(question="q")

    assert prediction.answer == "DONE"
    assert prediction.workflow_state == {}
    assert prediction.termination_reason == "submit"


def test_forward_publishes_retained_history_and_inputs() -> None:
    """The exact in-loop History remains available to trace/failure consumers."""
    ctx.install_trajectory_cell()
    agent = _build(_WsSig)
    lm = DummyLM(
        [
            {
                "next_thought": "done",
                "tool_calls": {
                    "tool_calls": [
                        {
                            "name": "submit",
                            "args": {"answer": "A", "workflow_state": {"k": 1}},
                        }
                    ]
                },
            }
        ]
    )

    with dspy.context(lm=lm, adapter=dspy.ChatAdapter()):
        agent(question="find alpha")

    retained = ctx.active_trajectory()
    assert isinstance(retained, dict)
    assert retained["input_args"] == {"question": "find alpha"}
    assert isinstance(retained["history"], list)
    assert len(retained["history"]) == 1
    assert retained["termination_reason"] == "submit"


def test_react_owner_exposes_no_out_of_loop_repair_methods() -> None:
    """CLIO's owner and production forward path contain no hidden repair call."""
    forward_source = inspect.getsource(reactv2_events.instrumented_forward)

    assert "_forced_submit(" not in forward_source
    assert "_forced_submit" not in _RetainingReActV2.__dict__
    assert "_bounded_submit_repair" not in _RetainingReActV2.__dict__
    assert not hasattr(reactv2, "reforce_submit_over_retained_history")
