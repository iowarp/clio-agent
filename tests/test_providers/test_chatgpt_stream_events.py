"""Unit tests for the raw Responses-event -> normalized-event mapping (A.5, A.9)."""

from __future__ import annotations

from clio_agent.providers.chatgpt.stream_events import (
    Completed,
    Failed,
    ReasoningDelta,
    ResponseEventParser,
    TextDelta,
    ToolCallDone,
)


def test_text_delta_events_are_forwarded() -> None:
    parser = ResponseEventParser()
    events = parser.feed({"type": "response.output_text.delta", "delta": "Hello"})
    assert events == [TextDelta("Hello")]


def test_empty_text_delta_is_dropped() -> None:
    parser = ResponseEventParser()
    assert parser.feed({"type": "response.output_text.delta", "delta": ""}) == []


def test_reasoning_summary_delta_forwarded() -> None:
    parser = ResponseEventParser()
    events = parser.feed({"type": "response.reasoning_summary_text.delta", "delta": "thinking..."})
    assert events == [ReasoningDelta("thinking...")]


def test_function_call_accumulates_arguments_and_emits_on_done() -> None:
    parser = ResponseEventParser()
    assert (
        parser.feed(
            {
                "type": "response.output_item.added",
                "output_index": 0,
                "item_id": "item_1",
                "item": {
                    "type": "function_call",
                    "id": "item_1",
                    "call_id": "call_1",
                    "name": "get_weather",
                },
            }
        )
        == []
    )
    assert (
        parser.feed(
            {
                "type": "response.function_call_arguments.delta",
                "item_id": "item_1",
                "delta": '{"city"',
            }
        )
        == []
    )
    assert (
        parser.feed(
            {
                "type": "response.function_call_arguments.delta",
                "item_id": "item_1",
                "delta": ': "SF"}',
            }
        )
        == []
    )
    events = parser.feed(
        {
            "type": "response.output_item.done",
            "output_index": 0,
            "item_id": "item_1",
            "item": {
                "type": "function_call",
                "id": "item_1",
                "call_id": "call_1",
                "name": "get_weather",
            },
        }
    )
    assert events == [
        ToolCallDone(call_id="call_1", name="get_weather", arguments='{"city": "SF"}', index=0)
    ]


def test_message_output_item_done_is_recorded_but_not_a_tool_call() -> None:
    parser = ResponseEventParser()
    events = parser.feed(
        {
            "type": "response.output_item.done",
            "item": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "hi"}],
            },
        }
    )
    assert events == []
    assert parser.output_items == [
        {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "hi"}]}
    ]


def test_response_completed_emits_completed_with_usage_and_output() -> None:
    parser = ResponseEventParser()
    events = parser.feed(
        {
            "type": "response.completed",
            "response": {
                "id": "resp_1",
                "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
                "output": [{"type": "message", "role": "assistant", "content": []}],
            },
        }
    )
    assert len(events) == 1
    completed = events[0]
    assert isinstance(completed, Completed)
    assert completed.response_id == "resp_1"
    assert completed.usage == {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}
    assert parser.completed is True
    assert parser.response_id == "resp_1"


def test_response_done_and_incomplete_are_normalized_like_completed() -> None:
    for event_type in ("response.done", "response.incomplete"):
        parser = ResponseEventParser()
        events = parser.feed({"type": event_type, "response": {"id": "resp_x", "usage": {}}})
        assert len(events) == 1
        assert isinstance(events[0], Completed)
        assert parser.completed is True


def test_response_failed_emits_failed_with_code_and_message() -> None:
    parser = ResponseEventParser()
    events = parser.feed(
        {
            "type": "response.failed",
            "response": {"error": {"code": "server_error", "message": "boom"}},
        }
    )
    assert events == [Failed(code="server_error", message="boom")]
    assert parser.completed is True


def test_bare_error_event_emits_failed() -> None:
    parser = ResponseEventParser()
    events = parser.feed({"type": "error", "code": "bad_request", "message": "nope"})
    assert events == [Failed(code="bad_request", message="nope")]


def test_unknown_event_type_is_ignored() -> None:
    parser = ResponseEventParser()
    assert parser.feed({"type": "response.some_future_event"}) == []
    assert parser.completed is False


def test_not_completed_until_a_terminal_event_arrives() -> None:
    parser = ResponseEventParser()
    parser.feed({"type": "response.output_text.delta", "delta": "partial"})
    assert parser.completed is False
