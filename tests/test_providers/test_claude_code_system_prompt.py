"""B4: the system prompt rides ``ClaudeAgentOptions.system_prompt``, never the query text.

The option names a file (``--system-prompt-file``): on the command line a clio system
prompt passes Windows' 32,767-character CreateProcess limit (found live 2026-10-02: the
spawn failed WinError 206, reported by the SDK as "Claude Code not found").

Through the real engine and pooled transport with a fake SDK: the request's system
prompt (plus the text tool rules when the request has tools) is the connected client's
``system_prompt`` option, and the query carries only the rendered messages. A request
with no system prompt leaves the option unset, so the CLI's own default applies.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from dspy.lm15 import FunctionTool, Message, Request

from clio_agent.providers import claude_code_engine
from clio_agent.providers.claude_code_options import build_sdk_options
from clio_agent.providers.claude_code_sessions import _reset_sessions_for_tests
from tests import _fake_claude_sdk as fake


@pytest.fixture(autouse=True)
def _clean_pool() -> Any:
    _reset_sessions_for_tests()
    claude_code_engine._CONVERSATIONS.clear_for_tests()
    yield
    _reset_sessions_for_tests()
    claude_code_engine._CONVERSATIONS.clear_for_tests()


def _system(options: Any) -> str:
    option = options.system_prompt
    assert option["type"] == "file"
    return Path(option["path"]).read_text(encoding="utf-8")


async def test_the_system_prompt_is_the_sdk_option_not_query_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sdk = fake.install(monkeypatch)
    await fake.drive(fake.request(Message.user("what?"), system="You are clio."))

    [client] = sdk.clients
    assert _system(client.options) == "You are clio."
    [(prompt, _session)] = client.queries
    assert prompt == "[user]\nwhat?"


async def test_tool_rules_ride_the_system_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    sdk = fake.install(monkeypatch)
    search = FunctionTool(
        name="search",
        description="Search.",
        parameters={"type": "object", "properties": {"q": {"type": "string"}}},
    )
    request = Request(
        model="claude_code/haiku",
        system="You are clio.",
        messages=(Message.user("what?"),),
        tools=(search,),
    )
    await fake.drive(request)

    system = _system(sdk.clients[0].options)
    assert system.startswith("You are clio.\n\n# How you act")
    assert "- search: Search." in system
    assert "# How you act" not in sdk.queries()[0][0]


async def test_no_system_prompt_leaves_the_option_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    sdk = fake.install(monkeypatch)
    await fake.drive(fake.request(Message.user("what?")))
    assert "system_prompt" not in sdk.clients[0].options.kwargs


def test_a_long_system_prompt_rides_a_file_not_the_command_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Red before: the whole prompt was the option (``--system-prompt <text>``) and the
    command line passed 32,767 characters; now the option is only the file's path."""
    fake.install(monkeypatch)
    prompt = "You are clio.
" + "- tool: does one thing well.
" * 4000
    options = build_sdk_options(
        model="sonnet", cwd=None, stream=True, thinking=None, system_prompt=prompt
    )
    assert len(prompt) > 32767
    assert len(json.dumps(options.system_prompt)) < 1000
    assert _system(options) == prompt
