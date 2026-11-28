"""How a run that raised is recorded: typed, with the failure's whole message."""

from __future__ import annotations

from clio_agent.flows.outcomes import CallFailed, run_ending


def test_an_unexpected_error_is_recorded_with_its_reason_and_type() -> None:
    status, result = run_ending(ValueError("could not parse the reply"))
    assert status == "flow_error"
    assert result == {
        "reason": "flow_error",
        "error": "ValueError",
        "message": "could not parse the reply",
    }


def test_a_long_message_reaches_the_parent_whole() -> None:
    """Agent data has no truncating caps: the parent reads the failure as it was."""
    message = "x" * 5000
    _, result = run_ending(RuntimeError(message))
    assert result["message"] == message


def test_a_typed_outcome_keeps_its_own_reason() -> None:
    status, result = run_ending(CallFailed("the researcher failed", handle="t1"))
    assert status == "call_failed"
    assert result["reason"] == "call_failed"
