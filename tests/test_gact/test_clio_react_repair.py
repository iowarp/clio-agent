"""Submit-schema and message contracts for ``ClioReAct``.

A declared output default is schema intent (droppable on ``submit``); a required
output without one is an in-loop submit rejection the model can retry. The returned
``Prediction`` carries the loop's typed messages (the task head, then one assistant
and one tool message per step) and its termination reason.
"""

from __future__ import annotations

from typing import Any

import dspy
import pytest

from clio_agent.gact.agents import clio_react
from clio_agent.gact.agents.clio_react import ClioReAct
from tests._scripted_engine import Reply, ScriptedEngine, calls, scripted_lm

pytestmark = pytest.mark.usefixtures("clio_core_plane")


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


def _submit_step(thought: str = "submit now", **args: Any) -> Reply:
    return calls(("submit", args), text=thought)


def _run(agent: ClioReAct, *steps: Reply) -> tuple[dspy.Prediction, ScriptedEngine]:
    lm, engine = scripted_lm(steps)
    with dspy.context(lm=lm):
        return agent(question="q"), engine


def _step_results(pred: dspy.Prediction, step: int) -> list[Any]:
    """The step's tool results: messages are the tool-use note and the head, then
    (assistant, tool) per step."""
    tool_message = pred.messages[3 + 2 * step]
    assert tool_message.role == "tool"
    return list(tool_message.parts)


def _text(result: Any) -> str:
    return "".join(getattr(p, "text", "") for p in result.content)


def test_declared_default_field_is_droppable_and_flows_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An author-declared default is schema intent, not runtime fabrication."""
    records = _capture_reasons(monkeypatch)
    pred, engine = _run(_build(_DefaultedSig), _submit_step(answer="ONLY_ANSWER"))

    assert (pred.answer, pred.workflow_state) == ("ONLY_ANSWER", {})
    assert pred.termination_reason == "submit"
    assert _step_results(pred, 0)[0].is_error is False
    assert len(engine.requests) == 1
    assert clio_react.REACT_SUBMIT_INVALID_OUTPUT not in _reasons(records)
    assert clio_react.REACT_SUBMIT_FIELD_SUPPRESSED in _reasons(records)


def test_required_field_without_default_is_rejected_then_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Missing required structured output is surfaced by the submit tool as an error
    observation; the model retries in-loop and the corrected submit completes."""
    records = _capture_reasons(monkeypatch)
    pred, engine = _run(
        _build(_WsSig),
        _submit_step(answer="ONLY_ANSWER"),
        _submit_step("fixed", answer="A", workflow_state={"ok": True}),
    )

    rejected = _step_results(pred, 0)[0]
    assert rejected.is_error is True
    assert "Missing required final output field(s): workflow_state" in _text(rejected)
    assert (pred.answer, pred.workflow_state) == ("A", {"ok": True})
    assert len(engine.requests) == 2
    assert clio_react.REACT_SUBMIT_INVALID_OUTPUT in _reasons(records)


def test_default_field_arg_schema_matches_declared_outputs() -> None:
    """The tool schema still advertises every structured output to the model."""
    submit = _build(_DefaultedSig).tools["submit"]

    assert set(submit.arg_types) == {"answer", "workflow_state"}
    assert submit.arg_types["workflow_state"] == dict[str, Any]


def test_prediction_carries_the_loop_messages() -> None:
    """The exact in-loop messages are on the returned Prediction for trace/failure
    consumers: the tool-use note and the task head, then the step's assistant and tool
    messages."""
    pred, _ = _run(_build(_WsSig), _submit_step("done", answer="A", workflow_state={"k": 1}))

    _note, head, step, results = pred.messages
    assert (head.role, head.parts[0].text) == ("user", "q")
    assert step.role == "assistant"
    assert step.parts[0].text == "done"
    assert [p.name for p in step.parts[1:]] == ["submit"]
    assert [r.name for r in results.parts] == ["submit"]
    assert pred.termination_reason == "submit"
