"""B4: the system message is split out for ``ClaudeAgentOptions.system_prompt``,
never serialized into the query text."""

from __future__ import annotations

import pytest

from clio_agent.providers.claude_code_system_prompt import (
    prepare_claude_request,
    split_system_prompt,
)


class _Unsupported(Exception):
    pass


def test_split_extracts_the_system_message_and_leaves_the_rest() -> None:
    system, remaining = split_system_prompt(
        [
            {"role": "system", "content": "You are CLIO."},
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        ],
        unsupported_multimodal_exc=_Unsupported,
    )
    assert system == "You are CLIO."
    assert remaining == [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
    ]


def test_split_joins_multiple_system_messages_in_order() -> None:
    system, remaining = split_system_prompt(
        [
            {"role": "system", "content": "first"},
            {"role": "user", "content": "hi"},
            {"role": "system", "content": "second"},
        ],
        unsupported_multimodal_exc=_Unsupported,
    )
    assert system == "first\n\nsecond"
    assert remaining == [{"role": "user", "content": "hi"}]


def test_split_returns_empty_string_when_no_system_message() -> None:
    system, remaining = split_system_prompt(
        [{"role": "user", "content": "hi"}], unsupported_multimodal_exc=_Unsupported
    )
    assert system == ""
    assert remaining == [{"role": "user", "content": "hi"}]


def test_split_is_case_insensitive_on_role() -> None:
    system, remaining = split_system_prompt(
        [{"role": "System", "content": "shout"}, {"role": "user", "content": "hi"}],
        unsupported_multimodal_exc=_Unsupported,
    )
    assert system == "shout"
    assert remaining == [{"role": "user", "content": "hi"}]


def test_split_rejects_an_image_part_in_a_system_message() -> None:
    """A system message is not exempt from the same multimodal refusal a user
    turn gets -- an image can't ride the ``--system-prompt`` CLI flag either."""
    with pytest.raises(_Unsupported):
        split_system_prompt(
            [
                {
                    "role": "system",
                    "content": [{"type": "image_url", "image_url": {"url": "x"}}],
                }
            ],
            unsupported_multimodal_exc=_Unsupported,
        )


def test_prepare_claude_request_wires_split_and_serialize_together() -> None:
    def serialize_text(messages: list[dict[str, object]]) -> str:
        return "|".join(str(m["content"]) for m in messages)

    system, prompt, native_blocks = prepare_claude_request(
        [
            {"role": "system", "content": "You are CLIO."},
            {"role": "user", "content": "hi"},
        ],
        serialize_text=serialize_text,
        unsupported_multimodal_exc=_Unsupported,
    )
    assert system == "You are CLIO."
    assert prompt == "hi"
    assert native_blocks == []
