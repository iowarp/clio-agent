"""Search questions and permission requests read as plain choices for the person."""

from __future__ import annotations

from types import SimpleNamespace

from clio_agent.gact.agent_elicitation import decide_routing
from clio_agent.gact.elicitation_schema import translate_form_schema
from clio_agent.search import broker
from clio_agent.tools.catalog import action_subject
from clio_agent.tools.servers.search_questions import input_required


def _question_field(decision: broker.Decision) -> dict:
    (request,) = input_required(decision).input_requests.values()
    return translate_form_schema(request.params.requested_schema).fields[0]


def test_approval_choice_carries_its_labels_to_the_form() -> None:
    field = _question_field(broker.approval("keenable"))
    assert field["enum"] == ["approve", "decline"]
    assert field["enum_names"] == ["Allow searches through Keenable", "Don't use Keenable"]


def test_mismatched_labels_are_not_used() -> None:
    schema = {
        "type": "object",
        "properties": {"choice": {"type": "string", "enum": ["a", "b"], "enumNames": ["A"]}},
    }
    assert translate_form_schema(schema).fields[0]["enum_names"] is None


def test_question_line_is_short_and_the_explanation_follows() -> None:
    for decision in (
        broker.approval("keenable"),
        broker.approval(broker.LITERATURE_APPROVAL),
        broker.exhausted("keenable", 1, 0.0, "exa"),
    ):
        first, _, rest = decision.message.partition("\n")
        assert first.endswith("?") and len(first) <= 80
        assert rest.strip()
    assert "ChemRxiv" in broker.approval(broker.LITERATURE_APPROVAL).message


def test_search_questions_go_to_the_person_without_a_fallback_reason() -> None:
    app = SimpleNamespace(state=SimpleNamespace())
    for namespace in ("web", "literature"):
        decision = decide_routing(
            app, mode="form", session_id="sess_x", namespace=namespace, audience="agent"
        )
        assert decision.route is False and not decision.reason


def test_permission_subject_follows_the_tools_annotations() -> None:
    assert action_subject("web_search") == "network tool"
    assert action_subject("literature_lookup") == "network tool"
    assert action_subject("fs_write_file") == "destructive tool"
    assert action_subject("some_unknown_tool") == "destructive tool"


def test_each_decision_in_one_call_has_its_own_input_key() -> None:
    import json as _json
    from types import SimpleNamespace

    from clio_agent.tools.servers import search_questions

    first = input_required(broker.approval("keenable"))
    second = input_required(broker.approval("exa"))
    assert set(first.input_requests).isdisjoint(second.input_requests)

    answered: list[tuple[str, str]] = []
    original = broker.apply_answer
    broker.apply_answer = lambda topic, value: answered.append((topic, value))
    try:
        (key,) = second.input_requests
        reply = SimpleNamespace(action="accept", content={"choice": "decline"})
        ctx = SimpleNamespace(
            input_responses={key: reply}, request_state=_json.dumps({"topic": "approve:exa"})
        )
        search_questions.apply_responses(ctx)
    finally:
        broker.apply_answer = original
    assert answered == [("approve:exa", "decline")]
