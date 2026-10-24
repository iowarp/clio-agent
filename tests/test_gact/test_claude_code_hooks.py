"""An imported Claude Code hook reads and answers on Claude Code's wire."""

from __future__ import annotations

import sys
from pathlib import Path

from clio_agent.gact.hooks.adapters import default_adapters
from clio_agent.gact.hooks.claude_code_hooks import CLAUDE_TOOL_NAMES, claude_envelope
from clio_agent.gact.hooks.config import HookEntry, HookMatch, HookRun
from clio_agent.gact.hooks.plugin_hooks import _entry
from clio_agent.gact.hooks.wire import HookEnvelope
from clio_agent.plugins.claude_code import TOOL_MAP

# The clio-dataset-report hook's shape: read tool_input.file_path, answer in
# hookSpecificOutput.additionalContext.
SCRIPT = """
import json, os, sys
event = json.load(sys.stdin)
path = event["tool_input"]["file_path"]
print(json.dumps({"hookSpecificOutput": {
    "hookEventName": event["hook_event_name"],
    "additionalContext": f"CHECK {event['tool_name']} {os.path.basename(path)} "
    + os.environ["CLAUDE_PLUGIN_ROOT"],
}}))
"""


def _envelope(path: str) -> HookEnvelope:
    return HookEnvelope(
        hook_event_name="PostToolUse",
        session_id="sess_1",
        cwd="/work",
        tool_name="fs_write_file",
        tool_input={"filepath": path, "content": "# Report\n"},
        payload={"is_error": False},
    )


def test_every_named_tool_maps_back_to_the_importers_tool_map() -> None:
    for clio, claude in CLAUDE_TOOL_NAMES.items():
        assert clio in TOOL_MAP[claude][0]


def test_the_edit_envelope_uses_claude_codes_edit_keys() -> None:
    envelope = HookEnvelope(
        hook_event_name="PreToolUse",
        session_id="sess_1",
        cwd="/work",
        tool_name="fs_edit_file",
        tool_input={
            "filepath": "/work/a.py",
            "old_string": "x = 1",
            "new_string": "x = 2",
            "replace_all": False,
        },
    )
    body = claude_envelope(envelope)
    assert body["tool_name"] == "Edit"
    assert body["tool_input"] == {
        "file_path": "/work/a.py",
        "old_string": "x = 1",
        "new_string": "x = 2",
        "replace_all": False,
    }


def test_the_search_envelope_uses_claude_codes_grep_keys() -> None:
    envelope = HookEnvelope(
        hook_event_name="PreToolUse",
        session_id="sess_1",
        cwd="/work",
        tool_name="fs_search_text",
        tool_input={"path": "/work", "query": "TODO", "file_glob": "*.py"},
    )
    body = claude_envelope(envelope)
    assert body["tool_name"] == "Grep"
    assert body["tool_input"] == {"path": "/work", "pattern": "TODO", "glob": "*.py"}


def test_the_envelope_uses_claude_code_names_and_keys() -> None:
    body = claude_envelope(_envelope("/work/out/dataset-report.md"))
    assert body["tool_name"] == "Write"
    assert body["tool_input"] == {
        "file_path": "/work/out/dataset-report.md",
        "content": "# Report\n",
    }
    assert body["tool_response"] == {"success": True}
    assert body["hook_event_name"] == "PostToolUse" and body["cwd"] == "/work"


def test_a_claude_code_hook_runs_and_its_context_reaches_clio(tmp_path: Path) -> None:
    script = tmp_path / "check.py"
    script.write_text(SCRIPT, encoding="utf-8")
    row = {
        "id": "clio-dataset-report.posttooluse-0-0",
        "event": "PostToolUse",
        "kind": "command",
        "command": sys.executable,
        "args": [str(script)],
        "tools": "fs_write_file|fs_edit_file",
        "dialect": "claude-code",
        "root": str(tmp_path),
        "plugin": "clio-dataset-report",
    }
    entry = _entry(row)
    assert entry.run.type == "claude-code"
    decision = default_adapters()[entry.run.type].invoke(entry, _envelope("/work/out/report.md"))
    assert decision.decision == "allow"
    assert decision.additional_context == f"CHECK Write report.md {tmp_path}"


def test_block_and_permission_decisions_map_to_clio(tmp_path: Path) -> None:
    script = tmp_path / "block.py"
    script.write_text(
        "import json\nprint(json.dumps({'decision': 'block', 'reason': 'stats differ'}))\n",
        encoding="utf-8",
    )
    entry = HookEntry(
        id="p.block",
        on=frozenset({"PostToolUse"}),
        match=HookMatch(),
        run=HookRun(type="claude-code", command=sys.executable, args=(str(script),)),
        source=str(tmp_path),
    )
    decision = default_adapters()["claude-code"].invoke(entry, _envelope("/work/x.md"))
    assert decision.decision == "deny" and decision.reason == "stats differ"
