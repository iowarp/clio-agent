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
    split = split_reply(reply, call_prefix="c", schemas=SCHEMAS)
    assert split.text == "Writing the parser."
    assert [(c.name, c.input) for c in split.calls] == [
        ("shell_bash", {"command": SCRIPT, "timeout": 30}),
        ("fs_read_file", {"path": "a.csv", "limit": 5}),
    ]


def test_the_x2_shapes_a_call_then_an_empty_or_equal_json_block_run_once(invoke: None) -> None:
    """The model's markup, then ``[]`` (as on X2) or the same call again as JSON: one call
    runs. SABOTAGE: refuse a JSON block beside the markup and these are unreadable steps."""
    markup = "<function_calls>\n" + _call("shell_bash", command="ls") + "</function_calls>\n\n"
    empty = split_reply(markup + "```tool_calls\n[]\n```", call_prefix="c", schemas=SCHEMAS)
    same = split_reply(
        markup + '```tool_calls\n[{"name": "shell_bash", "arguments": {"command": "ls"}}]\n```',
        call_prefix="c", schemas=SCHEMAS,
    )  # fmt: skip
    for split in (empty, same):
        assert [(c.name, c.input) for c in split.calls] == [("shell_bash", {"command": "ls"})]
        assert "tool_calls" not in split.text


def test_a_different_json_block_beside_the_markup_is_refused(invoke: None) -> None:
    reply = ("<function_calls>\n" + _call("shell_bash", command="ls") + "</function_calls>\n"
             '```tool_calls\n[{"name": "shell_bash", "arguments": {"command": "rm x"}}]\n```')  # fmt: skip
    split = split_reply(reply, call_prefix="c", schemas=SCHEMAS)
    assert [c.name for c in split.calls] == [INVALID_TOOL_CALL]


def test_a_typed_value_that_is_not_json_and_an_unclosed_block_are_refused(invoke: None) -> None:
    bad = (
        "<function_calls>\n"
        + _call("shell_bash", command="ls", timeout="soon")
        + "</function_calls>"
    )
    unclosed = "<function_calls>\n" + _call("shell_bash", command="ls")
    for reply in (bad, unclosed):
        [call] = split_reply(reply, call_prefix="c", schemas=SCHEMAS).calls
        assert call.name == INVALID_TOOL_CALL


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


def test_the_json_protocol_is_unchanged_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CLIO_TEXT_TOOL_PROTOCOL", raising=False)
    reply = "<function_calls>\n" + _call("shell_bash", command="ls") + "</function_calls>"
    assert all(c.name != "shell_bash" for c in split_reply(reply, call_prefix="c").calls)
    assert text_tools.turn_reminder() == text_tools.TURN_REMINDER


def test_a_block_cut_before_its_invoke_is_refused_in_this_protocols_terms(invoke: None) -> None:
    """On H4 a reply began at ``<parameter ...>``, its opening lines missing: there is no
    tool name, so nothing runs, and the reason names this protocol's block, not the JSON
    one (it said "require a complete ```tool_calls JSON block"). SABOTAGE: drop the
    ``_CUT`` branch and the JSON protocol's message comes back."""
    reply = '\n<parameter name="command">ls</parameter>\n</invoke>\n</function_calls>'
    [call] = split_reply(reply, call_prefix="c", schemas=SCHEMAS).calls
    assert call.name == INVALID_TOOL_CALL
    assert call.input.get("error") == invoke_calls.CUT_BLOCK
