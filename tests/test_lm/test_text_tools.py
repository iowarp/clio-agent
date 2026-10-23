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
    TURN_REMINDER,
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


@pytest.mark.parametrize("newline,indent", [("\n", ""), ("\r\n", "  ")])
def test_multiple_tool_blocks_never_silently_drop_earlier_calls(newline: str, indent: str) -> None:
    """The observed Claude spawn/read/ask response must execute none of its blocks."""
    names = ["spawn_agent_task", "fs_read_file", "ask_user"]
    blocks = [
        indent + _block([{"name": name, "arguments": {}}]).replace("\n", newline) for name in names
    ]
    reply = newline.join(["I will start the work.", *blocks, "All work was accepted."])
    parsed = split_reply(reply, call_prefix="observed", available_tools=names)
    assert parsed.text == "I will start the work."
    assert len(parsed.calls) == 1
    assert parsed.calls[0].name == INVALID_TOOL_CALL
    assert "multiple tool-call blocks" in parsed.calls[0].input["error"]
    assert "no operation was submitted" in parsed.calls[0].input["error"]
    assert len(parsed.calls[0].input["block"]) <= 4000
    assert all(name in parsed.calls[0].input["block"] for name in names)


def test_one_block_retains_every_parallel_call_in_its_original_order() -> None:
    """Rejecting multiple blocks must preserve the supported parallel-call contract."""
    names = ["spawn_agent_task", "fs_read_file", "ask_user"]
    parsed = split_reply(
        "Starting.\n"
        + _block(
            [{"name": name, "arguments": {"value": index}} for index, name in enumerate(names)]
        ),
        call_prefix="parallel",
        available_tools=names,
    )
    assert parsed.text == "Starting."
    assert [call.name for call in parsed.calls] == names
    assert [call.input for call in parsed.calls] == [{"value": index} for index in range(3)]


def test_system_carries_the_rules_and_every_tool_schema() -> None:
    system = render_system("You are clio.", [SEARCH])
    assert system.startswith("You are clio.\n\n# How you act")
    assert "NOT function calls of your own runtime" in system
    assert "# Available tools" in system
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


def test_orphaned_call_list_is_rejected_without_executing_it() -> None:
    raw = '[{"name":"search","arguments":{"query":"x"}}]\n```'
    split = split_reply(raw, call_prefix="t", available_tools={"search"})
    [call] = split.calls
    assert call.name == INVALID_TOOL_CALL
    assert isinstance(call.input["error"], str)
    assert "opening" in call.input["error"]
    assert call.input["block"] == raw
    assert not split.text


@pytest.mark.parametrize("size", [20, 5000])
def test_orphaned_single_call_is_a_bounded_error_not_an_operation(size: int) -> None:
    """A live fresh-restart reply had a closing fence but no opening fence or list."""
    raw = (
        json.dumps({"name": "restart_agent", "arguments": {"target": "x" * size, "fresh": True}})
        + "\n```"
    )
    split = split_reply(raw, call_prefix="t", available_tools={"restart_agent"})
    [call] = split.calls
    assert call.name == INVALID_TOOL_CALL
    assert call.input["block"] == raw[:4000]
    assert "opening" in str(call.input["error"])
    assert not split.text


@pytest.mark.parametrize(
    ("raw", "tools"),
    [
        pytest.param('{"name":"search","arguments":{}}\n```', set(), id="no-tools"),
        pytest.param('{"name":"other","arguments":{}}\n```', {"search"}, id="unknown"),
        pytest.param(
            '{"name":"search","arguments":{},"result":"data"}\n```',
            {"search"},
            id="data",
        ),
        pytest.param('{"name":"search","arguments":[]}\n```', {"search"}, id="arguments"),
        pytest.param('Example: {"name":"search","arguments":{}}\n```', {"search"}, id="example"),
        pytest.param(
            '```json\n{"name":"search","arguments":{}}\n```', {"search"}, id="fenced-example"
        ),
        pytest.param(
            '{"name":"search","arguments":{}}\n```\nMore text.', {"search"}, id="trailing-text"
        ),
    ],
)
def test_orphaned_single_call_data_and_examples_remain_answers(raw: str, tools: set[str]) -> None:
    """Reject only the complete declared call shape, never data or examples."""
    split = split_reply(raw, call_prefix="t", available_tools=tools)
    assert (split.text, split.calls) == (raw, [])


def test_corrupted_tool_fence_is_a_protocol_error_without_submitting_work() -> None:
    """The live Haiku reply replaced its opening fence with an underscore."""
    raw = '_tool_calls\n[{"name":"spawn_agent_task","arguments":{"agent":"worker"}}]\n```'
    split = split_reply(raw, call_prefix="t", available_tools={"spawn_agent_task"})
    [call] = split.calls
    assert call.name == INVALID_TOOL_CALL
    assert call.input["block"] == raw
    assert "opening" in str(call.input["error"])
    assert not split.text


def test_corrupted_delimiters_around_prose_are_a_bounded_error_not_a_call() -> None:
    """A live Haiku reply put a promise, rather than calls, inside broken delimiters."""
    raw = (
        "_tool_calls\nI'll spawn the worker once with the assignment as specified. "
        "The sentinel read happens in a separate step after the handle comes back.\n\n_tool_calls"
    )
    split = split_reply(raw, call_prefix="t", available_tools={"spawn_agent_task"})
    [call] = split.calls
    assert call.name == INVALID_TOOL_CALL
    assert call.input["block"] == raw
    assert "delimiters" in str(call.input["error"])
    assert not split.text


@pytest.mark.parametrize(
    ("raw", "tools"),
    [
        ("_tool_calls\nI'll spawn the worker.\n_tool_calls", set()),
        ("_tool_calls\nI'll spawn the worker.", {"spawn_agent_task"}),
        ("I'll spawn the worker.\n_tool_calls", {"spawn_agent_task"}),
        ("Example:\n_tool_calls\nI'll spawn the worker.\n_tool_calls", {"spawn_agent_task"}),
        ("```text\n_tool_calls\nI'll spawn the worker.\n_tool_calls\n```", {"spawn_agent_task"}),
        ("_tool_calls\nI'll spawn the worker.\n_tool_calls\nMore text.", {"spawn_agent_task"}),
        ("_tool_calls is not a valid tool fence.", {"spawn_agent_task"}),
    ],
)
def test_corrupted_delimiter_examples_and_partial_shapes_remain_answers(
    raw: str, tools: set[str]
) -> None:
    """Only a complete unquoted broken wrapper is treated as malformed protocol."""
    split = split_reply(raw, call_prefix="t", available_tools=tools)
    assert (split.text, split.calls) == (raw, [])


@pytest.mark.parametrize(
    ("raw", "tools"),
    [
        ('_tool_calls\n[{"name":"search","arguments":{}}]\n```', set()),
        ('_tool_calls\n[{"name":"other","arguments":{}}]\n```', {"search"}),
        ('_tool_calls\n[{"name":"search","arguments":{},"result":"data"}]\n```', {"search"}),
        ('_tool_calls\n[{"name":"search","arguments":{}}]', {"search"}),
        ('Example:\n_tool_calls\n[{"name":"search","arguments":{}}]\n```', {"search"}),
        ('```text\n_tool_calls\n[{"name":"search","arguments":{}}]\n```', {"search"}),
        ('_tool_calls\n[{"name":"search","arguments":{}}]\n```\nMore text.', {"search"}),
    ],
)
def test_corrupted_fence_data_and_examples_remain_answers(raw: str, tools: set[str]) -> None:
    """Only the whole observed shape for available tools can produce protocol feedback."""
    split = split_reply(raw, call_prefix="t", available_tools=tools)
    assert (split.text, split.calls) == (raw, [])


@pytest.mark.parametrize(
    "reply",
    [
        '[{"name":"search","arguments":{"query":"x"}}]',
        '[{"name":"other","arguments":{}}]\n```',
        '[{"name":"search","arguments":{},"result":"data"}]\n```',
        'Example:\n```json\n[{"name":"search","arguments":{}}]\n```',
        'Here is the requested data: [{"name":"search","arguments":{}}]\n```',
    ],
)
def test_json_answers_and_tool_examples_are_not_reinterpreted(reply: str) -> None:
    split = split_reply(reply, call_prefix="t", available_tools={"search"})
    assert split.text == reply
    assert not split.calls


def test_orphaned_call_shape_without_available_tools_remains_an_answer() -> None:
    raw = '[{"name":"search","arguments":{}}]\n```'
    split = split_reply(raw, call_prefix="t")
    assert (split.text, split.calls) == (raw, [])


@pytest.mark.parametrize("count", [1, 2])
def test_bare_tool_calls_envelope_is_a_protocol_error_not_an_operation(count: int) -> None:
    """A live Claude restart ended as unfenced call-shaped JSON instead of a call."""
    raw = json.dumps(
        {
            "tool_calls": [
                {"name": "restart_agent", "arguments": {"target": "task_old", "fresh": False}}
            ]
            * count
        }
    )
    split = split_reply(raw, call_prefix="t", available_tools={"restart_agent"})
    [call] = split.calls
    assert call.name == INVALID_TOOL_CALL
    assert call.input["block"] == raw
    assert "fenced" in str(call.input["error"])
    assert not split.text


@pytest.mark.parametrize(
    ("raw", "tools"),
    [
        ('{"tool_calls":[{"name":"search","arguments":{}}]}', set()),
        ('{"tool_calls":[{"name":"other","arguments":{}}]}', {"search"}),
        ('{"tool_calls":[]}', {"search"}),
        ('{"tool_calls":[{"name":"search","arguments":{}}],"result":"data"}', {"search"}),
        ('{"tool_calls":[{"name":"search","arguments":{},"result":"data"}]}', {"search"}),
        ('{"tool_calls":[{"name":"search","arguments":[]}]}', {"search"}),
        ('Example: {"tool_calls":[{"name":"search","arguments":{}}]}', {"search"}),
        ('```json\n{"tool_calls":[{"name":"search","arguments":{}}]}\n```', {"search"}),
    ],
)
def test_json_envelope_data_and_examples_remain_answers(raw: str, tools: set[str]) -> None:
    """Only the entire reserved envelope for declared tools is a protocol error."""
    split = split_reply(raw, call_prefix="t", available_tools=tools)
    assert (split.text, split.calls) == (raw, [])


@pytest.mark.parametrize("size", [20, 5000])
def test_bare_declared_call_object_is_a_bounded_error_not_an_operation(size: int) -> None:
    """The live Haiku cancellation spawn returned a call object without its fence/list."""
    raw = json.dumps(
        {"name": "spawn_agent_task", "arguments": {"agent": "worker", "task": "x" * size}}
    )
    split = split_reply(raw, call_prefix="t", available_tools={"spawn_agent_task"})
    assert len(split.calls) == 1
    [call] = split.calls
    assert call.name == INVALID_TOOL_CALL
    assert call.input["block"] == raw[:4000]
    assert "fenced" in str(call.input["error"])
    assert not split.text


@pytest.mark.parametrize(
    ("raw", "tools"),
    [
        pytest.param('{"name":"search","arguments":{}}', set(), id="no-tools"),
        pytest.param('{"name":"other","arguments":{}}', {"search"}, id="unknown-tool"),
        pytest.param('{"name":"search","arguments":{},"result":"data"}', {"search"}, id="data"),
        pytest.param('{"name":"search"}', {"search"}, id="missing-arguments"),
        pytest.param('{"name":"search","arguments":[]}', {"search"}, id="non-object-arguments"),
        pytest.param('Example: {"name":"search","arguments":{}}', {"search"}, id="example"),
        pytest.param(
            '```json\n{"name":"search","arguments":{}}\n```', {"search"}, id="fenced-example"
        ),
        pytest.param(
            '{"name":"search","arguments":{}} trailing text', {"search"}, id="trailing-text"
        ),
    ],
)
def test_json_call_data_and_examples_remain_answers(raw: str, tools: set[str]) -> None:
    """Only a whole exact call object for a declared tool requires protocol correction."""
    split = split_reply(raw, call_prefix="t", available_tools=tools)
    assert (split.text, split.calls) == (raw, [])


@pytest.mark.parametrize("count", [1, 2])
def test_xml_tool_calls_container_is_a_protocol_error_not_an_operation(count: int) -> None:
    """The actual Haiku parent used XML delimiters around an otherwise valid call list."""
    raw = (
        "<tool_calls>\n"
        + json.dumps(
            [
                {
                    "name": "message_agent",
                    "arguments": {"target": "sess_child", "message": "held", "wake": False},
                }
            ]
            * count
        )
        + "\n</tool_calls>"
    )
    split = split_reply(raw, call_prefix="t", available_tools={"message_agent"})
    [call] = split.calls
    assert call.name == INVALID_TOOL_CALL
    assert call.input["block"] == raw
    assert "XML" in str(call.input["error"])
    assert not split.text


@pytest.mark.parametrize("quote", ['"', "'"], ids=["double-quotes", "single-quotes"])
def test_xml_parameter_call_list_is_a_protocol_error_not_an_operation(quote: str) -> None:
    """Live Haiku emitted a complete parameter envelope without any invoke closer."""
    raw = (
        f"<parameter name={quote}tool_calls{quote}>\n"
        '[{"name":"observe_tasks","arguments":{"tasks":["task_actual"],"notify":true}}]'
        "\n</parameter>"
    )
    split = split_reply(raw, call_prefix="t", available_tools={"observe_tasks"})
    [call] = split.calls
    assert call.name == INVALID_TOOL_CALL
    assert call.input["block"] == raw
    assert "XML" in str(call.input["error"])
    assert not split.text


@pytest.mark.parametrize(
    ("raw", "tools"),
    [
        ('<parameter name="tool_calls">[{"name":"search","arguments":{}}]</parameter>', set()),
        ('<parameter name="tool_calls">[{"name":"other","arguments":{}}]</parameter>', {"search"}),
        ('<parameter name="q">[{"name":"search","arguments":{}}]</parameter>', {"search"}),
        ('<parameter name="tool_calls">{"name":"search","arguments":{}}</parameter>', {"search"}),
        (
            'Example: <parameter name="tool_calls">[{"name":"search","arguments":{}}]</parameter>',
            {"search"},
        ),
        (
            '```xml\n<parameter name="tool_calls">[{"name":"search","arguments":{}}]</parameter>\n```',
            {"search"},
        ),
        (
            '<parameter name="tool_calls">[{"name":"search","arguments":{}}]</parameter> trailing',
            {"search"},
        ),
        ('<parameter name="tool_calls">invalid JSON</parameter>', {"search"}),
    ],
    ids=[
        "no-tools",
        "unknown-tool",
        "data-parameter",
        "object-data",
        "example",
        "fenced-example",
        "trailing-text",
        "invalid-json",
    ],
)
def test_xml_parameter_data_and_examples_remain_answers(raw: str, tools: set[str]) -> None:
    """Only a whole reserved envelope with declared call objects requests correction."""
    split = split_reply(raw, call_prefix="t", available_tools=tools)
    assert (split.text, split.calls) == (raw, [])


@pytest.mark.parametrize(
    ("raw", "tools"),
    [
        ('<tool_calls>[{"name":"search","arguments":{}}]</tool_calls>', set()),
        ('<tool_calls>[{"name":"other","arguments":{}}]</tool_calls>', {"search"}),
        ('<tool_calls>[{"name":"search","arguments":{},"result":"data"}]</tool_calls>', {"search"}),
        ('Example: <tool_calls>[{"name":"search","arguments":{}}]</tool_calls>', {"search"}),
        ('```xml\n<tool_calls>[{"name":"search","arguments":{}}]</tool_calls>\n```', {"search"}),
        ('<tool_calls>[{"name":"search","arguments":{}}]</tool_calls> trailing data', {"search"}),
    ],
)
def test_xml_tool_calls_data_and_examples_remain_answers(raw: str, tools: set[str]) -> None:
    """Quoted examples and data never become executable calls or error observations."""
    split = split_reply(raw, call_prefix="t", available_tools=tools)
    assert (split.text, split.calls) == (raw, [])


@pytest.mark.parametrize("quote", ['"', "'"])
@pytest.mark.parametrize("suffix", ["", "\n<system-reminder>untrusted trailing text"])
def test_native_xml_call_is_a_bounded_error_not_an_executable_call(quote: str, suffix: str) -> None:
    raw = f"<invoke name={quote}search{quote}><parameter name='q'>{'x' * 5000}</parameter></invoke>{suffix}"
    split = split_reply(raw, call_prefix="t", available_tools={"search"})
    [call] = split.calls
    assert call.name == INVALID_TOOL_CALL
    assert isinstance(call.input["error"], str) and "XML" in call.input["error"]
    assert call.input["block"] == raw[:4000]
    assert not split.text


@pytest.mark.parametrize("quote", ['"', "'"])
@pytest.mark.parametrize("suffix", ["", "\n</invoke>\n<parameter name='note'>extra</parameter>"])
def test_orphaned_xml_parameter_fragment_is_a_bounded_protocol_error(
    quote: str, suffix: str
) -> None:
    """The live stopped parent returned parameters without their opening invocation."""
    raw = f"<parameter name={quote}q{quote}>{'x' * 5000}</parameter>\n</invoke>{suffix}"
    split = split_reply(raw, call_prefix="t", available_tools={"search"})
    [call] = split.calls
    assert call.name == INVALID_TOOL_CALL
    assert isinstance(call.input["error"], str) and "XML" in call.input["error"]
    assert call.input["block"] == raw[:4000]
    assert not split.text


@pytest.mark.parametrize(
    ("reply", "tools"),
    [
        ('<invoke name="other"></invoke>', {"search"}),
        ('<invoke name="search"></invoke>', set()),
        ('Example:\n<invoke name="search"></invoke>', {"search"}),
        ('```xml\n<invoke name="search"></invoke>\n```', {"search"}),
        ('<document name="search">ordinary XML data</document>', {"search"}),
        ('<parameter name="q">ordinary XML data</parameter>', {"search"}),
        ('<parameter name="q">x</parameter></invoke>', set()),
        ('```xml\n<parameter name="q">x</parameter></invoke>\n```', {"search"}),
        ('Example:\n<parameter name="q">x</parameter></invoke>', {"search"}),
    ],
)
def test_xml_data_and_examples_remain_answers(reply: str, tools: set[str]) -> None:
    split = split_reply(reply, call_prefix="t", available_tools=tools)
    assert (split.text, split.calls) == (reply, [])


def test_turn_reminder_requires_both_fences_without_opening_a_block() -> None:
    assert FENCE not in TURN_REMINDER
    assert "opening line" in TURN_REMINDER and "closing line" in TURN_REMINDER
    assert "JSON list alone or XML invoke/parameter tags are not calls" in TURN_REMINDER


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
    assert isinstance(call.input["error"], str)
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


def test_the_stream_does_not_show_a_call_also_written_as_invoke_markup() -> None:
    """Only the fenced block runs; the same call written as invoke markup in the prose
    is a duplicate the person should not read as text, wherever the chunks break."""

    reply = (
        "I'll inspect the data first.\n\n"
        '<invoke name="shell_bash"> <parameter name="command">ls</parameter> </invoke>\n\n'
        "Then I'll report.\n\n"
        '```tool_calls\n[{"name": "shell_bash", "arguments": {"command": "ls"}}]\n```'
    )
    for size in (1, 3, 7, len(reply)):
        splitter = StreamSplitter()
        shown = "".join(splitter.push(reply[i : i + size]) for i in range(0, len(reply), size))
        assert shown == "I'll inspect the data first.\n\n\n\nThen I'll report.", size
        assert "<" not in shown
        assert splitter.dropped_invoke_markup == 1


def test_the_stream_holds_an_unfinished_invoke_and_keeps_ordinary_angle_brackets() -> None:
    splitter = StreamSplitter()
    assert splitter.push("a < b and ") == "a < b and "
    assert splitter.push("<inv") == ""
    assert splitter.push('oke name="x">') == ""
    assert splitter.push("</invoke> done") == " done"
    assert splitter.dropped_invoke_markup == 1


def test_a_corrupted_fence_word_on_its_own_line_is_not_shown() -> None:
    """``_tool_calls`` (the fence without its backticks) beside a real call is protocol
    markup: never shown, counted for the audit, whatever the chunking."""
    reply = (
        "_tool_calls\nI'll remember that.\n_tool_call\n"
        '```tool_calls\n[{"name": "create_memory", "arguments": {}}]\n```'
    )
    for size in (1, 3, 7, len(reply)):
        splitter = StreamSplitter()
        shown = "".join(splitter.push(reply[i : i + size]) for i in range(0, len(reply), size))
        assert shown == "I'll remember that.", size
        assert splitter.dropped_fence_words == 2


def test_words_that_merely_start_like_the_fence_word_are_shown() -> None:
    splitter = StreamSplitter()
    assert splitter.push("_tool") == ""
    assert splitter.push("_calls_are documented here\n") == "_tool_calls_are documented here"
    assert splitter.dropped_fence_words == 0
