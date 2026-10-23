"""Run a hook imported from a Claude Code plugin on the wire it was written for.

A Claude Code hook script reads Claude Code's stdin envelope (``tool_name`` such as
``Write``, ``tool_input.file_path``) and answers in Claude Code's output shape
(``hookSpecificOutput.additionalContext``, ``decision: "block"``,
``permissionDecision``). The importer marks such hooks with the ``claude-code``
dialect; this adapter translates the envelope in and the answer out, so the script
sees what it expects and its answer reaches the turn.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any

from clio_agent.gact.hooks.adapters import SubprocessAdapter
from clio_agent.gact.hooks.config import HookEntry
from clio_agent.gact.hooks.events import POST_TOOL_USE, USER_PROMPT_EXPANSION
from clio_agent.gact.hooks.wire import (
    HookDecision,
    HookEnvelope,
    parse_hook_output,
    record_hook_reason,
)

#: CLIO tool -> the Claude Code tool a hook script knows it as. ``fs_write_file`` and
#: ``fs_edit_file`` take Claude Code's Write and Edit arguments, so a write guard on
#: ``Write|Edit`` reads the input it was written for.
CLAUDE_TOOL_NAMES: dict[str, str] = {
    "fs_read_file": "Read",
    "fs_glob": "Glob",
    "fs_grep": "Grep",
    "fs_write_file": "Write",
    "fs_edit_file": "Edit",
    "shell_bash": "Bash",
    "web_fetch": "WebFetch",
    "web_search": "WebSearch",
    "spawn_agent_task": "Task",
}

#: CLIO tool argument -> the Claude Code ``tool_input`` key. The write tools already use
#: Claude Code's ``content`` / ``old_string`` / ``new_string`` / ``replace_all``.
CLAUDE_INPUT_KEYS: dict[str, str] = {"filepath": "file_path"}

_DECISIONS = {"allow": "allow", "approve": "allow", "deny": "deny", "block": "deny", "ask": "ask"}


def claude_envelope(envelope: HookEnvelope, *, permission_mode: str = "default") -> dict[str, Any]:
    """The stdin envelope a Claude Code hook script reads."""

    body: dict[str, Any] = {
        "session_id": envelope.session_id,
        "transcript_path": "",
        "cwd": envelope.cwd,
        "permission_mode": permission_mode,
        "hook_event_name": envelope.hook_event_name,
    }
    if envelope.tool_name is not None:
        body["tool_name"] = CLAUDE_TOOL_NAMES.get(envelope.tool_name, envelope.tool_name)
        body["tool_input"] = {
            CLAUDE_INPUT_KEYS.get(key, key): value
            for key, value in (envelope.tool_input or {}).items()
        }
        if envelope.hook_event_name == POST_TOOL_USE:
            payload = envelope.payload or {}
            body["tool_response"] = {"success": not payload.get("is_error", False)}
    if envelope.prompt is not None:
        body["prompt"] = envelope.prompt
    if envelope.hook_event_name == USER_PROMPT_EXPANSION:
        payload = envelope.payload or {}
        body["expansion_type"] = "slash_command"
        body["command_name"] = str(payload.get("command_name") or "")
        body["command_args"] = str(payload.get("command_args") or "")
        body["command_source"] = "plugin"
    return body


def clio_output(raw: Mapping[str, Any], *, hook_id: str, event: str) -> dict[str, Any]:
    """Claude Code's hook answer in CLIO's output shape."""

    specific = raw.get("hookSpecificOutput")
    specific = specific if isinstance(specific, Mapping) else {}
    verdict = str(specific.get("permissionDecision") or raw.get("decision") or "allow")
    decision = _DECISIONS.get(verdict.lower())
    if decision is None:
        record_hook_reason("hook_unknown_decision", hook_id=hook_id, event=event, decision=verdict)
        decision = "allow"
    if raw.get("continue") is False:
        # Claude Code would end the whole run; a CLIO hook decides one call or turn.
        record_hook_reason("claude_code_continue_unsupported", hook_id=hook_id, event=event)
    return {
        "decision": decision,
        "reason": str(specific.get("permissionDecisionReason") or raw.get("reason") or ""),
        "additionalContext": str(
            specific.get("additionalContext") or raw.get("additionalContext") or ""
        ),
        "systemMessage": str(raw.get("systemMessage") or ""),
    }


class ClaudeCodeHookAdapter(SubprocessAdapter):
    """The subprocess wire, spoken in Claude Code's dialect."""

    def stdin_body(self, entry: HookEntry, envelope: HookEnvelope) -> dict[str, Any]:
        del entry
        return claude_envelope(envelope)

    def environment(self, entry: HookEntry, envelope: HookEnvelope) -> dict[str, str] | None:
        env = dict(os.environ)
        if entry.source:
            env["CLAUDE_PLUGIN_ROOT"] = entry.source
        if envelope.cwd:
            env["CLAUDE_PROJECT_DIR"] = envelope.cwd
        return env

    def decision(
        self, obj: dict[str, Any] | None, entry: HookEntry, envelope: HookEnvelope
    ) -> HookDecision:
        event = envelope.hook_event_name
        if obj is None:
            return parse_hook_output(None, hook_id=entry.id, event=event)
        raw = clio_output(obj, hook_id=entry.id, event=event)
        return parse_hook_output(raw, hook_id=entry.id, event=event)


__all__ = [
    "CLAUDE_INPUT_KEYS",
    "CLAUDE_TOOL_NAMES",
    "ClaudeCodeHookAdapter",
    "claude_envelope",
    "clio_output",
]
