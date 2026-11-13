"""The invoke protocol: the model's own call markup is the call, written once.

On X2 Claude wrote calls as ``<invoke>`` markup, hidden and never run, then again as a
JSON block or as an empty ``[]`` block (an unreadable step). Under
``lm.text_tool_protocol: invoke`` the markup is read as the call, with string
arguments raw and the rest JSON per the tool's schema.
"""

from __future__ import annotations

import pytest

from clio_agent.lm.engines import invoke_calls, text_tools
from clio_agent.lm.engines.text_tools import INVALID_TOOL_CALL, split_reply

SCHEMAS = {
    "shell_bash": {"properties": {"command": {"type": "string"},
                                  "timeout": {"type": "integer"}}},
    "fs_read_file": {"properties": {"path": {"type": "string"},
                                    "limit": {"anyOf": [{"type": "integer"}, {"type": "null"}]}}},
}  # fmt: skip
TOOLS = list(SCHEMAS)  # the agent's tools: an invoke of one of them is a call
SCRIPT = "cat > p.py <<'E'\nprint(\"a\\tb\")\nE\npython3 p.py"


@pytest.fixture
def invoke(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLIO_TEXT_TOOL_PROTOCOL", "invoke")


def _call(name: str, **params: str) -> str:
    body = "".join(f'<parameter name="{k}">{v}</parameter>\n' for k, v in params.items())
    return f'<invoke name="{name}">\n{body}</invoke>\n'


def test_a_script_is_one_raw_string_and_other_types_are_json(invoke: None) -> None:
    """SABOTAGE: read every parameter as text and ``timeout`` stays the string "30"."""
    reply = ("Writing the parser.\n<function_calls>\n" + _call("shell_bash", command=SCRIPT,
             timeout="30") + _call("fs_read_file", path="a.csv", limit="5") + "</function_calls>")  # fmt: skip
    split = split_reply(reply, call_prefix="c", available_tools=TOOLS, schemas=SCHEMAS)
    assert split.text == "Writing the parser."
    assert [(c.name, c.input) for c in split.calls] == [
        ("shell_bash", {"command": SCRIPT, "timeout": 30}),
        ("fs_read_file", {"path": "a.csv", "limit": 5}),
    ]


def test_the_x2_shapes_a_call_then_an_empty_or_equal_json_block_run_once(invoke: None) -> None:
    """The model's markup, then ``[]`` (as on X2) or the same call again as JSON: one call
    runs. SABOTAGE: refuse a JSON block beside the markup and these are unreadable steps."""
    markup = "<function_calls>\n" + _call("shell_bash", command="ls") + "</function_calls>\n\n"
    empty = split_reply(
        markup + "```tool_calls\n[]\n```", call_prefix="c", available_tools=TOOLS, schemas=SCHEMAS
    )
    same = split_reply(
        markup + '```tool_calls\n[{"name": "shell_bash", "arguments": {"command": "ls"}}]\n```',
        call_prefix="c", available_tools=TOOLS, schemas=SCHEMAS,
    )  # fmt: skip
    for split in (empty, same):
        assert [(c.name, c.input) for c in split.calls] == [("shell_bash", {"command": "ls"})]
        assert "tool_calls" not in split.text


def test_a_different_json_block_beside_the_markup_is_refused_or_never_run(
    invoke: None,
) -> None:
    """Before the call block, a different JSON block is refused: which to run is not the
    host's to guess. After it, it is overrun (written before any result existed): only
    the block runs. SABOTAGE: run every call written and ``rm x`` runs."""
    block = "<function_calls>\n" + _call("shell_bash", command="ls") + "</function_calls>\n"
    other = '```tool_calls\n[{"name": "shell_bash", "arguments": {"command": "rm x"}}]\n```'
    before = split_reply(other + "\n" + block, call_prefix="c", available_tools=TOOLS,
                         schemas=SCHEMAS)  # fmt: skip
    assert [c.name for c in before.calls] == [INVALID_TOOL_CALL]
    after = split_reply(block + other, call_prefix="c", available_tools=TOOLS, schemas=SCHEMAS)
    assert [(c.name, c.input) for c in after.calls] == [("shell_bash", {"command": "ls"})]
    assert "rm x" in after.overrun
    assert "rm x" not in after.text


def test_a_typed_value_that_is_not_json_and_an_unclosed_invoke_are_refused(invoke: None) -> None:
    bad = (
        "<function_calls>\n"
        + _call("shell_bash", command="ls", timeout="soon")
        + "</function_calls>"
    )
    unclosed = '<function_calls>\n<invoke name="shell_bash">\n<parameter name="command">ls'
    for reply in (bad, unclosed):
        [call] = split_reply(reply, call_prefix="c", available_tools=TOOLS, schemas=SCHEMAS).calls
        assert call.name == INVALID_TOOL_CALL


def test_the_container_is_decoration_lost_or_stray(invoke: None) -> None:
    """Across the A/B the ``<function_calls>`` opener was missing before 18 of 57 calls
    (the reply began at ``<invoke``) and once trailed the calls on its own, which was
    refused as an unclosed block. The complete ``<invoke>`` elements are the calls.
    SABOTAGE: read the block from the first ``<function_calls>`` and the stray one
    fails the call."""
    lost = "\n" + _call("shell_bash", command="ls") + "</function_calls>"
    stray = "\n" + _call("shell_bash", command="ls") + "\n\n\n<function_calls>"
    for reply in (lost, stray):
        split = split_reply(reply, call_prefix="c", available_tools=TOOLS, schemas=SCHEMAS)
        assert [(c.name, c.input) for c in split.calls] == [("shell_bash", {"command": "ls"})]
        assert split.text == ""


def test_without_a_block_a_json_block_is_still_read_and_plain_text_is_an_answer(
    invoke: None,
) -> None:
    fenced = '```tool_calls\n[{"name": "shell_bash", "arguments": {"command": "ls"}}]\n```'
    assert [c.name for c in split_reply(fenced, call_prefix="c").calls] == ["shell_bash"]
    assert split_reply("The peak is at 200.", call_prefix="c").calls == []


def test_the_protocol_is_described_and_echoed_in_its_own_form(invoke: None) -> None:
    from dspy.lm15 import FunctionTool, Message, TextPart, ToolCallPart

    system = text_tools.render_system("S", [FunctionTool("shell_bash", "Run", {"type": "object"})])
    assert "<function_calls>" in system and "```tool_calls" not in system
    assert text_tools.turn_reminder() == invoke_calls.INVOKE_REMINDER
    echoed = text_tools._render_message(
        Message(role="assistant", parts=(TextPart(text="Listing."),
                                         ToolCallPart(id="1", name="shell_bash",
                                                      input={"command": SCRIPT}))),
        text_tools._Media((), []),
    )  # fmt: skip
    assert f'<parameter name="command">{SCRIPT}</parameter>' in echoed


def test_the_json_protocol_reads_only_its_fenced_block(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLIO_TEXT_TOOL_PROTOCOL", "json")
    reply = "<function_calls>\n" + _call("shell_bash", command="ls") + "</function_calls>"
    assert all(c.name != "shell_bash" for c in split_reply(reply, call_prefix="c").calls)
    assert text_tools.turn_reminder() == text_tools.TURN_REMINDER


def test_a_block_cut_before_its_invoke_is_refused_in_this_protocols_terms(invoke: None) -> None:
    """On H4 a reply began at ``<parameter ...>``, its opening lines missing: there is no
    tool name, so nothing runs, and the reason names this protocol's block, not the JSON
    one (it said "require a complete ```tool_calls JSON block"). SABOTAGE: drop the
    ``_CUT`` branch and the JSON protocol's message comes back."""
    reply = '\n<parameter name="command">ls</parameter>\n</invoke>\n</function_calls>'
    [call] = split_reply(reply, call_prefix="c", available_tools=TOOLS, schemas=SCHEMAS).calls
    assert call.name == INVALID_TOOL_CALL
    assert call.input.get("error") == invoke_calls.CUT_BLOCK


def test_markup_that_is_data_is_never_a_call(invoke: None) -> None:
    """An ``<invoke>`` in a fenced example, one naming a tool the agent does not have,
    and any markup when the agent has no tools stay text. SABOTAGE: drop the fence
    filter and the example runs as a call."""
    known = ["shell_bash"]
    example = "Write it like this:\n```xml\n" + _call("shell_bash", command="rm -rf x") + "```"
    unknown = _call("other_tool", query="x")
    for reply in (example, unknown):
        split = split_reply(reply, call_prefix="c", available_tools=known, schemas=SCHEMAS)
        assert split.calls == [] and "<invoke" in split.text
    split = split_reply(_call("shell_bash", command="ls"), call_prefix="c")
    assert split.calls == []  # no tools: an answer


def test_the_stream_never_shows_call_markup_or_its_pieces(invoke: None) -> None:
    """L1's answers began with "</  </function_cal   </": a step's closing
    ``</function_calls>`` (its opener lost) streamed as text, in pieces. SABOTAGE: drop
    ``</?function_calls>`` from the splitter's openers and the pieces stream."""
    reply = ("Reading the shards.\\n" + _call("shell_bash", command="ls")
             + "</function_calls>\\nThe peak is at 200.")  # fmt: skip
    for size in (1, 3, 7, len(reply)):
        splitter = text_tools.StreamSplitter()
        shown = "".join(splitter.push(reply[i : i + size]) for i in range(0, len(reply), size))
        assert "<" not in shown and "function" not in shown, (size, shown)
        assert shown.startswith("Reading the shards.")


def test_namespaced_call_markup_is_read_and_never_shown(invoke: None) -> None:
    """Live (sonnet, claude_code, 2026-11-12) a step began with the parameter tag in its
    namespaced spelling, opener lost; nothing knew that spelling, so the cut call
    streamed and was stored as the step's text. A namespaced block is the call; a cut
    one is refused; neither streams. SABOTAGE: drop ``plain_call_tags`` and the cut
    script streams and comes back as the answer."""
    for ns in ("antml", "ns"):
        cut = f'\n<{ns}:parameter name="command">{SCRIPT}</parameter>'
        whole = (f'<{ns}:function_calls>\n<{ns}:invoke name="shell_bash">\n'
                 f'<{ns}:parameter name="command">ls</{ns}:parameter>\n'
                 f'</{ns}:invoke>\n</{ns}:function_calls>')  # fmt: skip
        [call] = split_reply(cut, call_prefix="c", available_tools=TOOLS, schemas=SCHEMAS).calls
        assert call.name == INVALID_TOOL_CALL
        assert call.input.get("error") == invoke_calls.CUT_BLOCK
        split = split_reply(
            "Listing.\n" + whole, call_prefix="c", available_tools=TOOLS, schemas=SCHEMAS
        )
        assert [(c.name, c.input) for c in split.calls] == [("shell_bash", {"command": "ls"})]
        assert split.text == "Listing."
        for reply in (cut, "Listing.\n" + whole):
            for size in (1, 3, 7, len(reply)):
                splitter = text_tools.StreamSplitter(TOOLS)
                shown = "".join(
                    splitter.push(reply[i : i + size]) for i in range(0, len(reply), size)
                )
                assert "<" not in shown and "parameter" not in shown, (ns, size, shown)
    splitter = text_tools.StreamSplitter(TOOLS)
    shown = "".join(splitter.push(c) for c in ("Use <b", "r> or <x:y", "> here."))
    assert shown == "Use <br> or <x:y> here."  # other tags only wait for their name


def test_a_loaded_deferred_tool_is_read_against_its_own_schema(invoke: None) -> None:
    """On L1R the agent loaded recall_context with search_tools and called it with
    ``ids=["cmp_..."]``: unlisted, it had no schema, so the list arrived as the string
    '["cmp_..."]' and ``limit`` as '10', and the tool refused both. SABOTAGE: pass a
    plain dict of the listed schemas (what the engine passed) and ``ids`` is a string
    again."""

    class _Recall:
        def format_as_litellm_function_call(self) -> dict:
            return {"function": {"parameters": {"properties": {
                "ids": {"type": "array", "items": {"type": "string"}},
                "query": {"type": "string"}, "limit": {"type": "integer"}}}}}  # fmt: skip

    reply = '<invoke name="recall_context">\n<parameter name="ids">["cmp_1"]</parameter>\n' \
        '<parameter name="limit">10</parameter>\n</invoke>'  # fmt: skip
    token = text_tools.CALLABLE_TOOL_DEFS.set({"recall_context": _Recall()})
    try:
        split = split_reply(reply, call_prefix="c", available_tools=[*TOOLS, "recall_context"],
                            schemas=text_tools.ToolSchemas(SCHEMAS))  # fmt: skip
    finally:
        text_tools.CALLABLE_TOOL_DEFS.reset(token)
    assert [(c.name, c.input) for c in split.calls] == [
        ("recall_context", {"ids": ["cmp_1"], "limit": 10})
    ]


@pytest.mark.parametrize(
    "tag", ["invoke_tool_placeholder", "invoke_tool_call_placeholder", "invoke_call_placeholder"]
)
def test_service_placeholders_for_a_call_are_refused_never_the_answer(
    invoke: None, tag: str
) -> None:
    """Live (haiku, claude_code, 2026-11-12, plain "Show me the first 10 rows of
    measurements.csv as a table"): the service replaced the model's call markup with
    ``<invoke_tool_call_placeholder>`` tags, a spelling only ``invoke_tool_placeholder``
    was known by, so those empty tags were the turn's whole answer. Any spelling of the
    family is a lost call: refused so the model sends it again, never shown. SABOTAGE:
    match one spelling again and the second case comes back as the answer."""
    reply = f"\n<{tag}>\n</{tag}>\n\n\n<{tag}>\n</{tag}>"
    split = split_reply(reply, call_prefix="c", available_tools=TOOLS, schemas=SCHEMAS)
    [call] = split.calls
    assert call.name == INVALID_TOOL_CALL
    assert call.input.get("error") == invoke_calls.CUT_BLOCK
    assert "placeholder" not in split.text
    for size in (1, 3, 7, len(reply)):
        splitter = text_tools.StreamSplitter(TOOLS)
        shown = "".join(splitter.push(reply[i : i + size]) for i in range(0, len(reply), size))
        assert "placeholder" not in shown and "<" not in shown, (size, shown)


def test_an_invented_result_tag_is_not_a_placeholder(invoke: None) -> None:
    """``<invoke_result>`` is not the service's substitution: it stays text (the overrun
    rules, not this one, handle a model that writes its own results)."""
    split = split_reply("<invoke_result>\nok\n</invoke_result>", call_prefix="c",
                        available_tools=TOOLS, schemas=SCHEMAS)  # fmt: skip
    assert split.calls == []


def test_a_block_repeating_one_identical_call_is_cut_after_its_last_whole_copy(
    invoke: None,
) -> None:
    same = _call("shell_bash", command="ls")
    varied = "".join(_call("shell_bash", command=f"ls {n}") for n in range(20))
    assert invoke_calls.repeated_call_end(varied, TOOLS) is None
    assert invoke_calls.repeated_call_end(same * 8, TOOLS) is None  # at the limit
    reply = "Loading.\n" + same * 30 + '<invoke name="shell_bash">\n<parameter'
    end = invoke_calls.repeated_call_end(reply, TOOLS)
    assert end is not None
    kept = reply[:end]
    assert kept.endswith("</invoke>") and kept.count("<invoke ") == 9
    split = split_reply(kept, call_prefix="c", available_tools=TOOLS, schemas=SCHEMAS)
    assert len(split.calls) == 9 and all(c.name == "shell_bash" for c in split.calls)
