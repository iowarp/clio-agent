"""Unit tests for the WebSocket delta-continuation computation (A.6, A.9)."""

from __future__ import annotations

from clio_agent.providers.codex.transport_ws import DeltaSnapshot, compute_delta


def _body(input_items: list[dict], **extra: object) -> dict:
    return {"model": "gpt-5.6-sol", "instructions": "be terse", "input": input_items, **extra}


def test_no_prior_snapshot_sends_full_body() -> None:
    body = _body([{"type": "message", "role": "user", "content": []}])
    result, is_delta = compute_delta(DeltaSnapshot(), body)
    assert is_delta is False
    assert result == body


def test_matching_prefix_sends_only_the_suffix() -> None:
    previous_input = [
        {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]}
    ]
    previous_output = [
        {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "hello"}],
        }
    ]
    previous = DeltaSnapshot(
        body=_body(previous_input), response_id="resp_1", output_items=previous_output
    )

    new_user_turn = {
        "type": "message",
        "role": "user",
        "content": [{"type": "input_text", "text": "again"}],
    }
    new_body = _body([*previous_input, *previous_output, new_user_turn])

    result, is_delta = compute_delta(previous, new_body)
    assert is_delta is True
    assert result["input"] == [new_user_turn]
    assert result["previous_response_id"] == "resp_1"
    assert "instructions" in result and result["instructions"] == new_body["instructions"]


def test_delta_strips_tool_outputs_from_expected_prefix() -> None:
    previous_input = [{"type": "message", "role": "user", "content": []}]
    previous_output = [
        {"type": "function_call", "call_id": "call_1", "name": "f", "arguments": "{}"},
    ]
    previous = DeltaSnapshot(
        body=_body(previous_input), response_id="resp_1", output_items=previous_output
    )

    # The caller's new input includes the function_call output CLIO appended --
    # that is NOT part of what the model produced, so the delta still applies
    # and only the truly new suffix (the tool result + next turn) is sent.
    tool_result = {"type": "function_call_output", "call_id": "call_1", "output": "42"}
    next_turn = {"type": "message", "role": "user", "content": []}
    new_body = _body([*previous_input, *previous_output, tool_result, next_turn])

    result, is_delta = compute_delta(previous, new_body)
    assert is_delta is True
    assert result["input"] == [tool_result, next_turn]


def test_prefix_mismatch_falls_back_to_full_body() -> None:
    previous_input = [
        {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]}
    ]
    previous = DeltaSnapshot(body=_body(previous_input), response_id="resp_1", output_items=[])

    # The new input does NOT start with previous input + previous output --
    # e.g. a rewind/edit changed history. Full body, continuation resets.
    different_input = [
        {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "different"}],
        }
    ]
    new_body = _body(different_input)

    result, is_delta = compute_delta(previous, new_body)
    assert is_delta is False
    assert result == new_body
    assert "previous_response_id" not in result


def test_changed_non_input_field_falls_back_to_full_body() -> None:
    previous_input = [{"type": "message", "role": "user", "content": []}]
    previous = DeltaSnapshot(
        body=_body(previous_input, reasoning={"effort": "medium"}),
        response_id="resp_1",
        output_items=[],
    )

    # Same input prefix, but a non-input field (reasoning effort) changed --
    # per A.6 this must NOT be treated as a delta.
    new_body = _body(previous_input, reasoning={"effort": "high"})
    result, is_delta = compute_delta(previous, new_body)
    assert is_delta is False
    assert result == new_body


def test_shorter_new_input_than_prefix_is_not_a_delta() -> None:
    previous_input = [
        {"type": "message", "role": "user", "content": []},
        {"type": "message", "role": "user", "content": []},
    ]
    previous = DeltaSnapshot(body=_body(previous_input), response_id="resp_1", output_items=[])
    new_body = _body(previous_input[:1])
    result, is_delta = compute_delta(previous, new_body)
    assert is_delta is False
    assert result == new_body


def test_no_response_id_yet_is_not_a_delta_even_with_a_body_snapshot() -> None:
    previous = DeltaSnapshot(body=_body([]), response_id="", output_items=[])
    new_body = _body([])
    result, is_delta = compute_delta(previous, new_body)
    assert is_delta is False
    assert result == new_body
