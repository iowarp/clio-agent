"""The text tool protocol of the single-prompt transports (Codex SDK, Claude Code SDK)."""

from __future__ import annotations

import json

import pytest
from dspy.lm15 import (
    DocumentPart,
    FunctionTool,
    ImagePart,
    Message,
    TextPart,
    ThinkingPart,
    ToolCallPart,
    ToolResultPart,
)

from clio_agent.lm.engines.text_tools import (
    FENCE,
    INVALID_TOOL_CALL,
    StreamSplitter,
    render_messages,
    render_system,
    split_reply,
)

SEARCH = FunctionTool(
    name="search",
    description="Search the corpus.",
    parameters={"type": "object", "properties": {"query": {"type": "string"}}},
)


def _block(calls: list[dict[str, object]]) -> str:
    return f"{FENCE}\n{json.dumps(calls)}\n```"


def test_system_carries_the_rules_and_every_tool_schema() -> None:
    system = render_system("You are clio.", [SEARCH])
    assert system.startswith("You are clio.\n\n# Calling tools")
    assert "- search: Search the corpus." in system
    assert json.dumps(SEARCH.parameters, sort_keys=True) in system


def test_system_without_tools_is_the_prompt_alone() -> None:
    assert render_system("You are clio.", []) == "You are clio."
    assert render_system(None, []) == ""


def test_messages_render_steps_and_results_by_call_id() -> None:
    messages = [
        Message.user("what?"),
        Message.assistant(
            [
                ThinkingPart(text="private"),
                TextPart(text="Looking."),
                ToolCallPart(id="c1", name="search", input={"query": "x"}),
            ]
        ),
        Message(
            role="tool",
            parts=(
                ToolResultPart(id="c1", name="search", content=(TextPart(text="hit"),)),
                ToolResultPart(
                    id="c2", name="search", content=(TextPart(text="boom"),), is_error=True
                ),
            ),
        ),
    ]
    text, images = render_messages(messages)
    assert images == []
    assert text == (
        "[user]\nwhat?\n\n"
        f"[assistant]\nLooking.\n{FENCE}\n"
        '[{"name": "search", "arguments": {"query": "x"}}]\n```\n\n'
        "[tool results]\n[c1 search]\nhit\n[c2 search (error)]\nboom"
    )
    assert "private" not in text


def test_images_ride_beside_the_text() -> None:
    image = ImagePart(data="aGk=", media_type="image/png")
    text, images = render_messages([Message(role="user", parts=(TextPart(text="see"), image))])
    assert images == [image]
    assert text == "[user]\nsee\n(image 1 attached)"


def test_a_part_the_transport_cannot_carry_is_a_typed_error() -> None:
    doc = DocumentPart(data="aGk=", media_type="application/pdf")
    with pytest.raises(TypeError, match="DocumentPart"):
        render_messages([Message(role="user", parts=(doc,))])


def test_reply_without_a_block_is_the_answer() -> None:
    split = split_reply("  The answer is 42.  ", call_prefix="t0")
    assert (split.text, split.calls) == ("The answer is 42.", [])


def test_reply_with_a_block_splits_text_and_calls() -> None:
    reply = "Searching both.\n" + _block(
        [
            {"name": "search", "arguments": {"query": "a"}},
            {"name": "search", "arguments": {"query": "b"}},
        ]
    )
    split = split_reply(reply, call_prefix="t3")
    assert split.text == "Searching both."
    assert split.calls == [
        ToolCallPart(id="t3_0", name="search", input={"query": "a"}),
        ToolCallPart(id="t3_1", name="search", input={"query": "b"}),
    ]


def test_text_after_the_block_stays_visible() -> None:
    reply = "Before.\n" + _block([{"name": "search", "arguments": {}}]) + "\nAfter."
    assert split_reply(reply, call_prefix="t").text == "Before.\n\nAfter."


def test_missing_arguments_default_to_an_empty_object() -> None:
    split = split_reply(_block([{"name": "search"}]), call_prefix="t")
    assert split.calls == [ToolCallPart(id="t_0", name="search", input={})]


@pytest.mark.parametrize(
    ("raw", "error"),
    [
        ("{not json", "Expecting"),
        ("[]", "non-empty JSON list"),
        ('{"name": "search"}', "non-empty JSON list"),
        ('["search"]', "must be an object"),
        ('[{"arguments": {}}]', "non-empty string 'name'"),
        ('[{"name": "search", "arguments": [1]}]', "must be a JSON object"),
    ],
)
def test_an_unreadable_block_is_one_invalid_call_never_repaired(raw: str, error: str) -> None:
    split = split_reply(f"Trying.\n{FENCE}\n{raw}\n```", call_prefix="t")
    [call] = split.calls
    assert (call.id, call.name) == ("t_0", INVALID_TOOL_CALL)
    assert error in call.input["error"]
    assert call.input["block"] == raw
    assert split.text == "Trying."


def test_the_stream_shows_text_and_holds_back_the_block() -> None:
    reply = "Looking it up.\n" + _block([{"name": "search", "arguments": {"query": "x"}}])
    splitter = StreamSplitter()
    shown = "".join(splitter.push(reply[i : i + 3]) for i in range(0, len(reply), 3))
    assert shown == "Looking it up."


def test_the_stream_holds_a_partial_fence_until_it_resolves() -> None:
    splitter = StreamSplitter()
    assert splitter.push("a ``") == "a "
    assert splitter.push("x") == "``x"
    assert splitter.push(" done") == " done"
