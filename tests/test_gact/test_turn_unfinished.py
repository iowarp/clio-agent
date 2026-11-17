"""A turn that stops without an answer says why on the message."""

from __future__ import annotations

from types import SimpleNamespace

from clio_agent.gact.turn_unfinished import unfinished_turn_error


def _state(**overrides: object) -> SimpleNamespace:
    values = {
        "sid": "sess_x",
        "sess": SimpleNamespace(routing_mode="auto"),
        "selected_agent": "ndp",
        "answer_text": "",
        "thinking_text": "",
        "nanoagents": [],
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_context_window_stop_after_tool_calls_is_a_typed_error() -> None:
    pred = SimpleNamespace(termination_reason="context_window_exceeded")
    error = unfinished_turn_error(_state(), pred, has_operation_output=True)
    assert error is not None and error.error == "context_window_exceeded"
    assert error.recoverable and error.details["termination_reason"] == "context_window_exceeded"


def test_context_window_stop_with_an_answer_keeps_the_answer() -> None:
    pred = SimpleNamespace(termination_reason="context_window_exceeded")
    assert unfinished_turn_error(_state(answer_text="done"), pred, True) is None


def test_nothing_visible_is_empty_response_and_tool_output_is_not() -> None:
    pred = SimpleNamespace(termination_reason="direct_response")
    assert unfinished_turn_error(_state(), pred, False).error == "empty_response"
    assert unfinished_turn_error(_state(), pred, True) is None
    yielded = SimpleNamespace(termination_reason="", assignment_disposition="yield")
    assert unfinished_turn_error(_state(), yielded, False) is None
