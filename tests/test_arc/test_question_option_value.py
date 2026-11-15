"""An agent that lists only labels gets its question asked, not refused."""

from __future__ import annotations

from clio_agent.arc.task_types import QuestionDefinition, QuestionOption


def test_an_option_without_a_value_answers_with_its_label() -> None:
    option = QuestionOption.model_validate({"label": "Keep it, order later", "description": "x"})
    assert option.value == "Keep it, order later"
    assert QuestionOption.model_validate({"label": "A", "value": "a"}).value == "a"
    question = QuestionDefinition.model_validate(
        {"prompt": "Which?", "kind": "choice", "options": [{"label": "One"}, {"label": "Two"}]}
    )
    assert [o.value for o in question.options] == ["One", "Two"]


def test_an_option_named_by_id_answers_with_its_id() -> None:
    option = QuestionOption.model_validate(
        {"id": "apt", "label": "Install with apt", "description": "Needs /usr"}
    )
    assert option.value == "apt"
    assert option.label == "Install with apt"
