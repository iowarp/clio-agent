"""How a run that raised is recorded: typed, and honest about a message it had to cut."""

from __future__ import annotations

from clio_agent.flows.outcomes import FLOW_ERROR_MESSAGE_CHARS, CallFailed, run_ending


def test_an_unexpected_error_is_recorded_with_its_reason_and_type() -> None:
    status, result = run_ending(ValueError("could not parse the reply"))
    assert status == "flow_error"
    assert result == {
        "reason": "flow_error",
        "error": "ValueError",
        "message": "could not parse the reply",
    }


def test_a_message_past_the_bound_says_how_much_was_cut() -> None:
    """Live 2026-11-17: a planner parse error carried the model's whole reply and the
    run card showed 2,000 characters of JSON that stopped mid-object, with no sign it
    had been cut."""
    message = "x" * (FLOW_ERROR_MESSAGE_CHARS + 345)
    _, result = run_ending(RuntimeError(message))
    assert result["message"] == message[:FLOW_ERROR_MESSAGE_CHARS]
    assert result["message_truncated_chars"] == 345


def test_a_typed_outcome_keeps_its_own_reason() -> None:
    status, result = run_ending(CallFailed("the researcher failed", handle="t1"))
    assert status == "call_failed"
    assert result["reason"] == "call_failed"
    assert "message_truncated_chars" not in result
