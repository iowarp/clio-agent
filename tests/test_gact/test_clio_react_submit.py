"""Pins for the #878 contract on the ``ClioReAct`` ``submit`` path.

``ClioReAct`` has no ``extract`` step: the final outputs ride the reserved ``submit``
tool's typed args and flow to the returned ``Prediction`` unchanged. These tests pin
that and its no-silent-fallback records:

* the submit-args VALUE flows to the final Prediction -- present (answer +
  workflow_state), absent (a required field the model omits), and rejected
  (invalid-typed workflow_state, which must NOT flow);
* each produced final-output field records a ``react_submit_field_suppressed`` reason
  (its value routes to the return contract, not a visible text lane), and a rejected
  submit records ``react_submit_invalid_output`` -- breaking the flow turns the
  value-flow assertions red.
"""

from __future__ import annotations

import json
from typing import Any

import dspy
import pytest

from clio_agent.gact.agents import clio_react
from clio_agent.gact.agents.clio_react import ClioReAct
from tests._scripted_engine import Reply, calls, scripted_lm

pytestmark = pytest.mark.usefixtures("clio_core_plane")


def _search(q: str) -> str:
    """A deterministic search tool."""
    return "R"


class _WsSig(dspy.Signature):
    question: str = dspy.InputField()
    answer: str = dspy.OutputField()
    workflow_state: dict[str, Any] = dspy.OutputField()


def _build(signature: Any = "question -> answer", max_iters: int = 4) -> ClioReAct:
    return ClioReAct(signature, tools=[dspy.Tool(_search, name="search")], max_iters=max_iters)


def _capture_reasons(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Capture the stream-audit records the loop emits (the sink is otherwise a
    configured-only no-op)."""
    records: list[dict[str, Any]] = []

    def _sink(stage: str, **fields: Any) -> None:
        records.append({"stage": stage, **fields})

    monkeypatch.setattr("clio_agent.runtime.stream_audit.stream_audit", _sink)
    return records


def _reasons(records: list[dict[str, Any]]) -> list[str]:
    return [r.get("duplicate_reason") for r in records]


def _step(name: str, thought: str = "step", **args: Any) -> Reply:
    return calls((name, args), text=thought)


def _run(agent: ClioReAct, *steps: Reply) -> dspy.Prediction:
    lm, _ = scripted_lm(steps)
    with dspy.context(lm=lm):
        return agent(question="q")


def _first_result(pred: dspy.Prediction) -> Any:
    """The first step's first tool result (messages: note, head, assistant, tool, ...)."""
    return pred.messages[3].parts[0]


def _text(result: Any) -> str:
    return "".join(getattr(p, "text", "") for p in result.content)


# --- 1. present: answer + valid workflow_state flow + are recorded -------------


def test_submit_present_values_flow_and_are_recorded(monkeypatch: pytest.MonkeyPatch) -> None:
    records = _capture_reasons(monkeypatch)
    pred = _run(
        _build(_WsSig),
        _step("submit", answer="DONE", workflow_state={"status": "complete"}),
    )

    # VALUE flows to the Prediction (the return contract).
    assert pred.answer == "DONE"
    assert pred.workflow_state == {"status": "complete"}
    assert pred.termination_reason == "submit"
    # Each produced final-output field is recorded as routed-to-contract (not visible).
    suppressed = [
        r for r in records if r.get("duplicate_reason") == clio_react.REACT_SUBMIT_FIELD_SUPPRESSED
    ]
    assert {r["field"] for r in suppressed} == {"answer", "workflow_state"}
    assert all(r.get("duplicate_suppressed") is True for r in suppressed)


# --- 2. absent: an omitted required output field is a rejected degraded step ---


def test_submit_absent_required_field_rejected_and_recorded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``submit`` requires every declared output field without a default, so a model
    that omits ``workflow_state`` yields an error result -- the value does NOT flow
    and the degraded step is recorded (never silently defaulted)."""
    records = _capture_reasons(monkeypatch)
    pred = _run(_build(_WsSig, max_iters=1), _step("submit", answer="ONLY_ANSWER"))

    assert pred.termination_reason == "max_iters"
    assert "answer" not in pred
    assert _first_result(pred).is_error is True
    assert clio_react.REACT_SUBMIT_INVALID_OUTPUT in _reasons(records)
    assert clio_react.REACT_SUBMIT_FIELD_SUPPRESSED not in _reasons(records)


# --- 3. invalid-typed workflow_state is rejected (value does NOT flow) ---------


def test_valid_submit_values_line_up() -> None:
    """A valid submit yields the final outputs and the step's result carries them --
    break the flow and this goes red."""
    pred = _run(_build(_WsSig), _step("submit", answer="A", workflow_state={"k": "v"}))
    assert (pred.answer, pred.workflow_state) == ("A", {"k": "v"})
    result = _first_result(pred)
    assert result.is_error is False
    assert json.loads(_text(result)) == {"answer": "A", "workflow_state": {"k": "v"}}


def test_invalid_typed_workflow_state_rejected_and_recorded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    records = _capture_reasons(monkeypatch)
    # workflow_state typed dict, but the model passes a string -> submit tool rejects.
    pred = _run(
        _build(_WsSig, max_iters=1), _step("submit", answer="A", workflow_state="NOT_A_DICT")
    )
    # The value did NOT flow (no silent acceptance of a bad-typed output).
    assert "answer" not in pred and "workflow_state" not in pred
    assert _first_result(pred).is_error is True
    # The degraded step is recorded, never silent...
    assert clio_react.REACT_SUBMIT_INVALID_OUTPUT in _reasons(records)
    # ...and it did NOT masquerade as a successful suppression.
    assert clio_react.REACT_SUBMIT_FIELD_SUPPRESSED not in _reasons(records)


def test_non_submit_calls_are_not_audited(monkeypatch: pytest.MonkeyPatch) -> None:
    """A plain tool call (not submit) records no submit reason."""
    records = _capture_reasons(monkeypatch)
    _run(_build(_WsSig, max_iters=1), _step("search", q="x"))
    assert clio_react.REACT_SUBMIT_FIELD_SUPPRESSED not in _reasons(records)
    assert clio_react.REACT_SUBMIT_INVALID_OUTPUT not in _reasons(records)
