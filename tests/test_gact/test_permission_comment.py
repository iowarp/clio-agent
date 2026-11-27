"""A person's comment on a permission answer reaches the agent with the decision (G19)."""

from __future__ import annotations

from clio_agent.gact.permission_comment import AllowDecision, person_decision
from clio_agent.gact.permission_gate import DenyDecision

CALL = {"tool_call": {"tool_name": "shell_bash", "input": {"command": "rm -r out"}}}


def test_a_deny_with_a_comment_is_the_reason_the_agent_reads() -> None:
    decision = person_decision({**CALL, "action": "deny", "comment": "keep out/, it is the run"})
    assert decision == "deny" and isinstance(decision, DenyDecision)
    assert decision.deny_message == (
        'The person declined "Run command" and said: keep out/, it is the run'
    )


def test_an_allow_with_a_comment_carries_it_as_a_note() -> None:
    decision = person_decision({**CALL, "action": "allow_session", "comment": " only today "})
    assert decision == "allow" and isinstance(decision, AllowDecision)
    assert decision.note == 'The person allowed "Run command" and commented: only today'


def test_an_answer_without_a_comment_names_the_action_as_the_card_did() -> None:
    allowed = person_decision({**CALL, "action": "allow", "comment": "  "})
    denied = person_decision({**CALL, "action": "deny"})
    assert (type(allowed), allowed) == (str, "allow")
    assert isinstance(denied, DenyDecision)
    assert denied.deny_message == 'The person declined "Run command"'


def test_the_card_title_is_what_the_agent_reads() -> None:
    row = {**CALL, "action": "deny", "summary": "Let the browser load example.org"}
    denied = person_decision(row)
    assert isinstance(denied, DenyDecision)
    assert denied.deny_message == 'The person declined "Let the browser load example.org"'
