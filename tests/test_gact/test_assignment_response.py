"""Conversation results preserve public evidence separately from final values."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import dspy

from clio_agent.arc.task_types import AssignmentDetails
from clio_agent.flows.values import CallResult, render_input
from clio_agent.gact.assignment_response import response_parts
from clio_agent.gact.conversation_assignments import assignment_outputs
from clio_agent.gact.parts import Part
from clio_agent.gact.turn_finalize import turn_outputs


class _History:
    def __init__(self, parts: list[dict[str, Any]]) -> None:
        self.parts = parts

    def read_content(self, reference: Any) -> Any:
        return {"answer": "Need clarification", "response_parts": self.parts}


def _part(identity: str, text: str, **metadata: Any) -> Part:
    return Part(id=identity, type="text", agent_id="researcher", text=text, metadata=metadata)


def test_public_response_retains_evidence_without_replacing_the_final_value() -> None:
    packet = "A sourced finding with its measurement and limitation.\n" * 350
    parts = [
        Part(id="private", type="thinking", agent_id="researcher", text="private reasoning"),
        _part("packet", packet, signature_field_name="next_thought"),
        Part(id="child", type="text", agent_id="another-agent", text="other author"),
        _part("addendum", "Provenance confirmed.", signature_field_name="answer"),
    ]
    state = SimpleNamespace(
        pred=dspy.Prediction(answer="Provenance confirmed."),
        transcript=SimpleNamespace(snapshot=lambda: parts),
        selected_agent="researcher",
        invocation_agent_id="researcher",
        active_agent_id="researcher",
        tools_called=[],
        assistant_metadata={"workflow_state": {"count": {"value": 42}}},
    )
    outputs = turn_outputs(state)
    result = CallResult(outputs)
    assert result.answer == result["answer"] == "Provenance confirmed."
    assert result.workflow_state == {"count": {"value": 42}}
    assert result.response_text == packet + "\n\nProvenance confirmed."
    assert [part["id"] for part in outputs["response_parts"]] == ["packet", "addendum"]
    assert render_input(result) == result.response_text
    assert "private reasoning" not in result.response_text
    assert "other author" not in result.response_text


def test_revisions_keep_the_original_inspectable_and_only_present_the_revision() -> None:
    parts = [
        _part("original", "Outdated conclusion", superseded_by="revision"),
        _part("revision", "Corrected conclusion", revises="original", revised_by="checker"),
    ]
    projected = response_parts(parts, {"researcher"})
    result = CallResult({"answer": "Outdated conclusion", "response_parts": projected})
    assert result.answer == "Outdated conclusion"
    assert result.response_text == "Corrected conclusion"
    assert projected[0]["text"] == "Outdated conclusion"
    assert projected[1]["metadata"]["revises"] == "original"


def test_response_parts_survive_a_question_yield_and_do_not_duplicate_on_reentry() -> None:
    earlier = response_parts([_part("first", "The findings before the question.")], {"researcher"})
    later = response_parts([_part("last", "The answer after clarification.")], {"researcher"})

    result = assignment_outputs(
        _History(earlier),
        AssignmentDetails.model_construct(last_output="previous"),
        {"answer": "Final value", "response_parts": [*earlier, *later]},
    )
    assert [part["id"] for part in result["response_parts"]] == ["first", "last"]
    assert CallResult(result).response_text == (
        "The findings before the question.\n\nThe answer after clarification."
    )
    assert result["answer"] == "Final value"


def test_plain_results_and_scalar_final_values_keep_their_existing_contract() -> None:
    for value, expected in [({"answer": "42"}, "42"), (42, "42"), (None, "")]:
        result = CallResult(value)
        assert result.response_text == result.answer == expected
    result = CallResult({"answer": "42", "response_parts": []})
    assert int(result.answer) == 42
    assert result.response_text == ""
