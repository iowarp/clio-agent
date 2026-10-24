"""``UserPromptExpansion``: a typed slash command's expansion reaches plugin hooks.

A Claude Code plugin's write guard (WTF-P's, for one) arms on this event, so its
PreToolUse check knows which roots the command may write to.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from clio_agent.gact import user_command_turn
from clio_agent.gact.hooks.claude_code_hooks import claude_envelope
from clio_agent.gact.hooks.config import HookMatch
from clio_agent.gact.hooks.events import USER_PROMPT_EXPANSION, is_deny_capable
from clio_agent.gact.hooks.wire import HookEnvelope, HookOutcome
from clio_agent.plugins.claude_code import import_claude_plugin
from tests.test_plugins.helpers import write


def _expansion(command_name: str) -> HookEnvelope:
    return HookEnvelope(
        hook_event_name=USER_PROMPT_EXPANSION,
        session_id="sess_1",
        cwd="/work",
        prompt="Write the methods section.",
        payload={"command_name": command_name, "command_args": "methods"},
    )


def test_a_claude_plugins_expansion_hook_keeps_its_command_matcher(tmp_path: Path) -> None:
    hooks = {
        "hooks": {
            "UserPromptExpansion": [
                {
                    "matcher": "^demo:(write-section|quick)$",
                    "hooks": [{"type": "command", "command": "node guard.js arm"}],
                }
            ]
        }
    }
    root = tmp_path / "demo"
    write(root / ".claude-plugin" / "plugin.json", json.dumps({"name": "demo"}))
    write(root / "hooks" / "hooks.json", json.dumps(hooks))

    record = import_claude_plugin(root)

    (hook,) = record.hooks
    assert hook.event == USER_PROMPT_EXPANSION
    assert hook.tools == "^demo:(write-section|quick)$"
    assert not [d for d in record.diagnostics if d.code == "hook_event_unmapped"]


def test_the_hook_matches_the_command_name_and_can_stop_it() -> None:
    match = HookMatch(tool=re.compile("^(?:^demo:(write-section|quick)$)$"))
    assert match.matches(_expansion("demo:write-section"))
    assert not match.matches(_expansion("demo:outline"))
    assert is_deny_capable(USER_PROMPT_EXPANSION)


def test_the_claude_code_envelope_names_the_command() -> None:
    body = claude_envelope(_expansion("demo:write-section"))
    assert body["hook_event_name"] == "UserPromptExpansion"
    assert body["command_name"] == "demo:write-section"
    assert body["command_args"] == "methods"
    assert body["prompt"] == "Write the methods section."
    assert "tool_name" not in body


def _submit(monkeypatch: pytest.MonkeyPatch, outcome: HookOutcome) -> tuple[dict, list[dict]]:
    seen: list[dict[str, Any]] = []
    accepted: list[dict[str, Any]] = []

    def dispatch(name: str, args: str, prompt: str, **kwargs: Any) -> HookOutcome:
        seen.append({"name": name, "args": args, "prompt": prompt, **kwargs})
        return outcome

    async def accept(app: Any, deps: Any, sid: str, submission: Any, **kwargs: Any) -> Any:
        accepted.append(kwargs)
        return SimpleNamespace(message_id="m1", delivery="start", state="running"), 202

    monkeypatch.setattr(user_command_turn, "dispatch_user_prompt_expansion", dispatch)
    monkeypatch.setattr(user_command_turn, "accept_message_async", accept)
    monkeypatch.setattr(user_command_turn, "command_cwd_for_request", lambda app, sid: "/work")
    result = asyncio.run(
        user_command_turn.submit_user_command(
            SimpleNamespace(),
            SimpleNamespace(),
            "sess_1",
            cmd_id="/write-section",
            user_input="methods",
            rendered="Write the methods section.",
            command_meta={"plugin": "demo", "command_source": "plugin"},
        )
    )
    assert seen == [
        {
            "name": "demo:write-section",
            "args": "methods",
            "prompt": "Write the methods section.",
            "session_id": "sess_1",
            "cwd": "/work",
        }
    ]
    return result, accepted


def test_context_a_hook_adds_reaches_the_model_with_the_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, accepted = _submit(
        monkeypatch, HookOutcome(additional_context="Writes are confined to manuscript/.")
    )
    assert result["result"]["type"] == "turn"
    assert accepted[0]["internal_model_text"] == (
        "Write the methods section.\n\nWrites are confined to manuscript/."
    )


def test_a_denying_hook_stops_the_command_with_its_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, accepted = _submit(monkeypatch, HookOutcome(decision="deny", reason="not now"))
    assert accepted == []
    assert result["result"] == {"type": "refused", "reason": "hook_denied", "message": "not now"}
