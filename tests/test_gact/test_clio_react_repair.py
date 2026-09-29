"""Submit-schema and History contracts for ``ClioReAct``.

A declared output default is schema intent (droppable on ``submit``); a required
output without one is an in-loop submit rejection the model can retry. The returned
``Prediction`` carries the loop's History (task inputs, then one event per step) and
its termination reason.
"""

from __future__ import annotations

from typing import Any

import dspy
import pytest
from dspy.utils.dummies import DummyLM

from clio_agent.gact.agents import clio_react
from clio_agent.gact.agents.clio_react import ClioReAct


def _search(q: str) -> str:
    """A deterministic search tool."""
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


def _build(signature: Any = "question -> answer", max_iters: int = 4) -> ClioReAct:
    return ClioReAct(signature, tools=[dspy.Tool(_search, name="search")], max_iters=max_iters)


def _capture_reasons(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []

    def sink(stage: str, **fields: Any) -> None:
        records.append({"stage": stage, **fields})

    monkeypatch.setattr("clio_agent.runtime.stream_audit.stream_audit", sink)
    return records


def _reasons(records: list[dict[str, Any]]) -> list[str]:
    return [str(record.get("duplicate_reason") or "") for record in records]


def _submit_step(thought: str = "submit now", **args: Any) -> dict[str, Any]:
    return {
        "next_thought": thought,
        "tool_calls": {"tool_calls": [{"name": "submit", "args": args}]},
    }


def _run(agent: ClioReAct, *steps: dict[str, Any]) -> tuple[dspy.Prediction, DummyLM]:
    lm = DummyLM([dict(s) for s in steps])
    with dspy.context(lm=lm, adapter=dspy.ChatAdapter()):
        return agent(question="q"), lm


def _step_results(pred: dspy.Prediction, step: int) -> list[Any]:
    return pred.history.messages[step + 1]["tool_calls"].tool_call_results.tool_call_results


def test_declared_default_field_is_droppable_and_flows_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An author-declared default is schema intent, not runtime fabrication."""
    records = _capture_reasons(monkeypatch)
    pred, lm = _run(_build(_DefaultedSig), _submit_step(answer="ONLY_ANSWER"))

    assert (pred.answer, pred.workflow_state) == ("ONLY_ANSWER", {})
    assert pred.termination_reason == "submit"
    assert _step_results(pred, 0)[0].is_error is False
    assert len(lm.history) == 1
    assert clio_react.REACT_SUBMIT_INVALID_OUTPUT not in _reasons(records)
    assert clio_react.REACT_SUBMIT_FIELD_SUPPRESSED in _reasons(records)


def test_required_field_without_default_is_rejected_then_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Missing required structured output is surfaced by the submit tool as an error
    observation; the model retries in-loop and the corrected submit completes."""
    records = _capture_reasons(monkeypatch)
    pred, lm = _run(
        _build(_WsSig),
        _submit_step(answer="ONLY_ANSWER"),
        _submit_step("fixed", answer="A", workflow_state={"ok": True}),
    )

    rejected = _step_results(pred, 0)[0]
    assert rejected.is_error is True
    assert "Missing required final output field(s): workflow_state" in rejected.value
    assert (pred.answer, pred.workflow_state) == ("A", {"ok": True})
    assert len(lm.history) == 2
    assert clio_react.REACT_SUBMIT_INVALID_OUTPUT in _reasons(records)


def test_default_field_arg_schema_matches_declared_outputs() -> None:
    """The tool schema still advertises every structured output to the model."""
    submit = _build(_DefaultedSig).tools["submit"]

    assert set(submit.arg_types) == {"answer", "workflow_state"}
    assert submit.arg_types["workflow_state"] == dict[str, Any]


def test_prediction_carries_the_loop_history_and_inputs() -> None:
    """The exact in-loop History is on the returned Prediction for trace/failure
    consumers: the task inputs first, then one event per step."""
    pred, _ = _run(_build(_WsSig), _submit_step("done", answer="A", workflow_state={"k": 1}))

    assert pred.history.messages[0] == {"question": "q"}
    assert len(pred.history.messages) == 2
    step = pred.history.messages[1]
    assert step["next_thought"] == "done"
    assert [c.name for c in step["tool_calls"].tool_calls] == ["submit"]
    assert pred.termination_reason == "submit"
