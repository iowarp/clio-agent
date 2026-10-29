"""An assignment's result reports the tool calls of every turn it spanned."""

from __future__ import annotations

from typing import Any

from clio_agent.arc.task_types import AssignmentDetails
from clio_agent.gact.conversation_assignments import assignment_outputs


class _History:
    """Retained content by reference, as the task history reads it."""

    def __init__(self, content: dict[str, Any]) -> None:
        self.content = content

    def read_content(self, reference: Any) -> Any:
        return self.content[reference]


def _details(last_output: Any = None) -> AssignmentDetails:
    return AssignmentDetails.model_construct(last_output=last_output)


def test_a_proposal_made_before_a_yield_survives_the_turn_that_completes() -> None:
    """Live L4: the critic proposed a step, yielded on a question, then finished."""

    proposal = {"name": "workflow_add_step", "args": {"step_id": "verify"}}
    history = _History({"first": {"answer": "asked", "tools_called": [proposal]}})
    final = {"answer": "audit done", "tools_called": [{"name": "web_fetch"}]}
    merged = assignment_outputs(history, _details("first"), final)
    assert merged["answer"] == "audit done"
    assert merged["tools_called"] == [proposal, {"name": "web_fetch"}]


def test_a_first_turn_or_an_earlier_turn_without_calls_is_unchanged() -> None:
    final = {"answer": "done", "tools_called": [{"name": "a"}]}
    assert assignment_outputs(_History({}), _details(), final) is final
    history = _History({"first": {"error": "empty_response"}})
    assert assignment_outputs(history, _details("first"), final) is final
