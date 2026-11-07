"""WTF-P's real write guard on CLIO's Claude Code hook wire: arm, deny, allow, disarm.

The guard arms on ``UserPromptExpansion`` for ``wtfp:write-section``, denies a
Write outside ``paper/`` and ``.planning/`` with its own reason, allows one inside,
and removes its marker on ``Stop``.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from clio_agent.gact.hooks.adapters import default_adapters
from clio_agent.gact.hooks.events import USER_PROMPT_EXPANSION
from clio_agent.gact.hooks.plugin_hooks import _entry
from clio_agent.gact.hooks.wire import HookEnvelope

GUARD = Path(__file__).resolve().parents[1] / "fixtures" / "wtfp_write_guard"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node runs the guard")


def _run(mode: str, event: str, envelope: HookEnvelope, root: Path):
    row = {
        "id": f"wtfp.{mode}",
        "event": event,
        "kind": "command",
        "command": "node",
        "args": [str(root / "wtfp-write-guard.js"), mode],
        "tools": "",
        "dialect": "claude-code",
        "root": str(root),
        "plugin": "wtfp",
    }
    entry = _entry(row)
    return default_adapters()[entry.run.type].invoke(entry, envelope)


def test_the_guard_confines_a_write_section_to_its_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "plugin"
    shutil.copytree(GUARD, root)
    work = tmp_path / "paper-project"
    work.mkdir()
    monkeypatch.setenv("TMPDIR", str(tmp_path / "tmp"))
    marker = tmp_path / "tmp" / "wtfp-write-guard" / "wtfp-write-guard-sess_guard.json"

    def write(path: str) -> HookEnvelope:
        return HookEnvelope(
            hook_event_name="PreToolUse",
            session_id="sess_guard",
            cwd=str(work),
            tool_name="fs_write_file",
            tool_input={"path": path, "content": "x"},
        )

    armed = _run(
        "arm",
        USER_PROMPT_EXPANSION,
        HookEnvelope(
            hook_event_name=USER_PROMPT_EXPANSION,
            session_id="sess_guard",
            cwd=str(work),
            prompt="Write the introduction.",
            payload={"command_name": "wtfp:write-section", "command_args": "introduction"},
        ),
        root,
    )
    assert marker.is_file()
    assert "file writes are confined to" in (armed.additional_context or "")

    outside = _run("check", "PreToolUse", write(str(work / "notes" / "copy.md")), root)
    assert outside.decision == "deny"
    assert "WTF-P write guard: /wtfp:write-section may write only under" in (outside.reason or "")

    inside = _run("check", "PreToolUse", write(str(work / "paper" / "introduction.tex")), root)
    assert inside.decision == "allow"

    _run(
        "disarm",
        "Stop",
        HookEnvelope(hook_event_name="Stop", session_id="sess_guard", cwd=str(work)),
        root,
    )
    assert not marker.exists()
    after = _run("check", "PreToolUse", write(str(work / "notes" / "copy.md")), root)
    assert after.decision == "allow"


def test_an_unguarded_command_does_not_arm(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "plugin"
    shutil.copytree(GUARD, root)
    monkeypatch.setenv("TMPDIR", str(tmp_path / "tmp"))
    _run(
        "arm",
        USER_PROMPT_EXPANSION,
        HookEnvelope(
            hook_event_name=USER_PROMPT_EXPANSION,
            session_id="sess_guard",
            cwd=str(tmp_path),
            prompt="Outline.",
            payload={"command_name": "wtfp:create-outline", "command_args": ""},
        ),
        root,
    )
    assert not (tmp_path / "tmp" / "wtfp-write-guard").exists()
