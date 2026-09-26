"""Unit tests for OpenAI-message <-> Responses-API conversion (A.5, A.9)."""

from __future__ import annotations

import pytest

from clio_agent.providers.codex.responses import (
    build_request_body,
    chat_messages_to_responses_input,
    chat_tools_to_responses_tools,
    extract_reasoning_items,
    with_reasoning_items,
)


def test_system_and_developer_messages_become_instructions() -> None:
    instructions, items = chat_messages_to_responses_input(
        [
            {"role": "system", "content": "be terse"},
            {"role": "developer", "content": "prefer bullet points"},
            {"role": "user", "content": "hello"},
        ]
    )
    assert instructions == "be terse\n\nprefer bullet points"
    assert items == [
        {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hello"}]}
    ]


def test_user_message_with_image_parts() -> None:
    _instructions, items = chat_messages_to_responses_input(
        [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "what is this?"},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}},
                ],
            }
        ]
    )
    assert items == [
        {
            "type": "message",
            "role": "user",
            "content": [
                {"type": "input_text", "text": "what is this?"},
                {"type": "input_image", "image_url": "data:image/png;base64,abc"},
            ],
        }
    ]


def test_assistant_tool_calls_become_function_call_items() -> None:
    _instructions, items = chat_messages_to_responses_input(
        [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_1",
                        "function": {"name": "get_weather", "arguments": '{"city": "SF"}'},
                    }
                ],
            }
        ]
    )
    assert items == [
        {
            "type": "function_call",
            "call_id": "call_1",
            "name": "get_weather",
            "arguments": '{"city": "SF"}',
        }
    ]


def test_assistant_message_with_text_and_tool_calls() -> None:
    _instructions, items = chat_messages_to_responses_input(
        [
            {
                "role": "assistant",
                "content": "Let me check.",
                "tool_calls": [{"id": "call_1", "function": {"name": "f", "arguments": "{}"}}],
            }
        ]
    )
    assert items == [
        {"type": "function_call", "call_id": "call_1", "name": "f", "arguments": "{}"},
        {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "Let me check."}],
        },
    ]


def test_tool_message_becomes_function_call_output() -> None:
    _instructions, items = chat_messages_to_responses_input(
        [{"role": "tool", "tool_call_id": "call_1", "content": "72F and sunny"}]
    )
    assert items == [
        {"type": "function_call_output", "call_id": "call_1", "output": "72F and sunny"}
    ]


def test_chat_tools_to_responses_tools_flattens_function_shape() -> None:
    tools = [
        {
            "type": "function",
            "function": {
                "name": "get_weather",
                "description": "Get the weather",
                "parameters": {"type": "object", "properties": {"city": {"type": "string"}}},
            },
        }
    ]
    converted = chat_tools_to_responses_tools(tools)
    assert converted == [
        {
            "type": "function",
            "name": "get_weather",
            "description": "Get the weather",
            "parameters": {"type": "object", "properties": {"city": {"type": "string"}}},
        }
    ]


def test_chat_tools_to_responses_tools_handles_none_and_empty() -> None:
    assert chat_tools_to_responses_tools(None) == []
    assert chat_tools_to_responses_tools([]) == []


def test_extract_reasoning_items_filters_by_type() -> None:
    output = [
        {"type": "message", "role": "assistant", "content": []},
        {"type": "reasoning", "id": "rs_1", "encrypted_content": "opaque"},
    ]
    assert extract_reasoning_items(output) == [
        {"type": "reasoning", "id": "rs_1", "encrypted_content": "opaque"}
    ]


def test_extract_reasoning_items_empty_when_none_present() -> None:
    assert extract_reasoning_items([{"type": "message", "role": "assistant", "content": []}]) == []
    assert extract_reasoning_items(None) == []


def test_with_reasoning_items_inserts_before_last_assistant_run() -> None:
    reasoning_item = {"type": "reasoning", "id": "rs_1", "encrypted_content": "opaque"}
    items = [
        {"type": "message", "role": "user", "content": []},
        {"type": "function_call", "call_id": "c1", "name": "f", "arguments": "{}"},
        {"type": "message", "role": "assistant", "content": []},
    ]
    result = with_reasoning_items(items, [reasoning_item])
    assert result == [items[0], reasoning_item, items[1], items[2]]


def test_with_reasoning_items_no_op_when_empty() -> None:
    items = [{"type": "message", "role": "user", "content": []}]
    assert with_reasoning_items(items, []) is items


def test_with_reasoning_items_prepends_when_no_assistant_turn_yet() -> None:
    reasoning_item = {"type": "reasoning", "id": "rs_1"}
    items = [{"type": "message", "role": "user", "content": []}]
    result = with_reasoning_items(items, [reasoning_item])
    assert result == [reasoning_item, items[0]]


def test_build_request_body_sets_required_a5_fields() -> None:
    body = build_request_body(
        model="gpt-5.6-sol",
        input_items=[{"type": "message", "role": "user", "content": []}],
        instructions="be terse",
        tools=None,
        tool_choice=None,
        session_id="sess_1",
        reasoning_effort=None,
    )
    assert body["store"] is False
    assert body["stream"] is True
    assert body["instructions"] == "be terse"
    assert body["tool_choice"] == "auto"
    assert body["parallel_tool_calls"] is True
    assert body["include"] == ["reasoning.encrypted_content"]
    assert body["prompt_cache_key"] == "sess_1"
    assert "tools" not in body
    assert "reasoning" not in body


def test_build_request_body_includes_tools_and_reasoning_effort() -> None:
    tools = [{"type": "function", "name": "f", "description": "", "parameters": {}}]
    body = build_request_body(
        model="gpt-5.6-sol",
        input_items=[],
        instructions="",
        tools=tools,
        tool_choice="auto",
        session_id="sess_1",
        reasoning_effort="high",
    )
    assert body["tools"] == tools
    assert body["reasoning"] == {"effort": "high", "summary": "auto"}


# --------------------------------------------------------------------------- #
# PDF attachments -> Responses ``input_file`` (verified live 2026-09-26).
# --------------------------------------------------------------------------- #

_PDF_URL = "data:application/pdf;base64,JVBERi0xLjQK"


def test_pdf_file_part_becomes_input_file() -> None:
    """DSPy's ``dspy.File`` part shape is delivered, not dropped."""

    _instructions, items = chat_messages_to_responses_input(
        [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "summarize"},
                    {"type": "file", "file": {"file_data": _PDF_URL, "filename": "paper.pdf"}},
                ],
            }
        ]
    )
    assert items[0]["content"] == [
        {"type": "input_text", "text": "summarize"},
        {"type": "input_file", "filename": "paper.pdf", "file_data": _PDF_URL},
    ]


def test_non_pdf_or_id_only_file_parts_are_refused_loudly() -> None:
    from clio_agent.providers.codex.errors import CodexUnsupportedInputError

    for file in (
        {"file_data": "data:text/csv;base64,YSxi", "filename": "t.csv"},
        {"file_id": "file-abc"},
    ):
        with pytest.raises(CodexUnsupportedInputError):
            chat_messages_to_responses_input(
                [{"role": "user", "content": [{"type": "file", "file": file}]}]
            )


def test_oversized_pdf_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    from clio_agent.providers.codex.errors import CodexUnsupportedInputError

    monkeypatch.setenv("CLIO_RESOURCE_NATIVE_DOCUMENT_MAX_BYTES", "4")
    with pytest.raises(CodexUnsupportedInputError, match="per-document ceiling"):
        chat_messages_to_responses_input(
            [{"role": "user", "content": [{"type": "file", "file": {"file_data": _PDF_URL}}]}]
        )
