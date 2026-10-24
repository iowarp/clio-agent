"""The agent writes files itself (megachange Phase 7).

``fs_write_file`` / ``fs_edit_file`` change the file on disk. They are writes, so the
permission gate asks or allows by the session's approval mode and PreToolUse hooks see
every write, including a Claude Code plugin's ``Write|Edit`` guard. Each successful
write is recorded as an applied ``file_diff`` the person can undo through
``POST /v1/sessions/{sid}/diffs/revert``, which refuses with a typed reason when the
file changed since the write or no previous content was kept.
"""

from __future__ import annotations

import hashlib
import json
import sys
import threading
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from clio_agent.gact.app import build_app
from clio_agent.gact.hooks import HookDispatcher, install_global_dispatcher
from clio_agent.gact.hooks.plugin_hooks import _entry
from clio_agent.gact.permission_gate import DenyDecision, _make_permission_gate
from clio_agent.gact.runtime.globals import _gact_app_context, _tool_session_context
from clio_agent.plugins.claude_code import import_claude_plugin
from clio_agent.tools.execution import tool_workspace_context
from clio_agent.tools.fs_edit import UNDO_CAPTURE_MAX_BYTES

from ._writing_agent import WritingAgent, workspace_session
from .conftest import complete_turn

pytestmark = pytest.mark.usefixtures("host_agent_executor")


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    monkeypatch.setenv("CLIO_ALLOWED_ROOTS", str(tmp_path))
    return workspace


# --------------------------------------------------------------------------- #
# The permission gate treats the write tools as writes
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("tool", ["fs_write_file", "fs_edit_file"])
def test_gate_asks_in_default_mode_and_allows_in_auto_edits(
    tmp_path: Path, root: Path, tool: str
) -> None:
    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as client:
        sid = workspace_session(client, root, approval_mode="ask")
        gate = _make_permission_gate(app)
        args = {"filepath": str(root / "a.txt"), "content": "x"}
        decision: list[str] = []

        def _call() -> None:
            with _gact_app_context(app), _tool_session_context(sid):
                decision.append(gate(tool, args))

        worker = threading.Thread(target=_call, daemon=True)
        worker.start()
        row = _wait_pending(app)
        assert row["tool_call"]["tool_name"] == tool
        assert client.post(f"/v1/permissions/{row['id']}", json={"action": "deny"}).status_code
        worker.join(timeout=10)
        assert decision == ["deny"]

        app.state.sessions.update(sid, approval_mode="auto-edits")
        with _gact_app_context(app), _tool_session_context(sid):
            assert gate(tool, args) == "allow"


def _wait_pending(app: Any) -> dict[str, Any]:
    import time

    deadline = time.time() + 10
    while time.time() < deadline:
        rows = [r for r in app.state.permissions.values() if r.get("status") == "pending"]
        if rows:
            return rows[0]
        time.sleep(0.02)
    raise AssertionError("the gate never asked")


# --------------------------------------------------------------------------- #
# A Claude Code plugin's PreToolUse guard on Write|Edit sees and blocks the write
# --------------------------------------------------------------------------- #

GUARD = """
import json, sys
event = json.load(sys.stdin)
with open(sys.argv[1], "a", encoding="utf-8") as seen:
    seen.write(json.dumps({"tool_name": event["tool_name"], "tool_input": event["tool_input"]}))
    seen.write("\\n")
print(json.dumps({"hookSpecificOutput": {
    "hookEventName": "PreToolUse",
    "permissionDecision": "deny",
    "permissionDecisionReason": "WTF-P: writes to protected/ are blocked",
}}))
"""


def _guard_plugin(tmp_path: Path, seen: Path) -> HookDispatcher:
    plugin = tmp_path / "guard-plugin"
    (plugin / ".claude-plugin").mkdir(parents=True)
    (plugin / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": "write-guard", "version": "1.0.0"}), encoding="utf-8"
    )
    (plugin / "scripts").mkdir()
    (plugin / "scripts" / "guard.py").write_text(GUARD, encoding="utf-8")
    (plugin / "hooks").mkdir()
    (plugin / "hooks" / "hooks.json").write_text(
        json.dumps(
            {
                "hooks": {
                    "PreToolUse": [
                        {
                            "matcher": "Write|Edit",
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": f"{sys.executable} "
                                    "${CLAUDE_PLUGIN_ROOT}/scripts/guard.py "
                                    f"{seen}",
                                }
                            ],
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    record = import_claude_plugin(plugin)
    [hook] = record.hooks
    assert hook.tools == "fs_write_file|fs_edit_file"
    row = {
        "id": hook.id,
        "event": hook.event,
        "kind": hook.kind,
        "command": hook.command,
        "args": list(hook.args),
        "tools": hook.tools,
        "dialect": "claude-code",
        "root": str(plugin),
        "plugin": "write-guard",
    }
    return HookDispatcher([_entry(row)])


def test_claude_code_write_guard_receives_the_write_and_its_deny_blocks_it(
    tmp_path: Path, root: Path
) -> None:
    seen = tmp_path / "seen.jsonl"
    target = root / "protected.txt"
    target.write_text("original\n", encoding="utf-8")
    agent = WritingAgent()
    agent.calls = [
        ("fs_write_file", {"filepath": str(target), "content": "clobbered\n"}),
        (
            "fs_edit_file",
            {"filepath": str(target), "old_string": "original", "new_string": "edited"},
        ),
    ]
    app = build_app(sessions_path=tmp_path / "s.json", agent=agent)
    install_global_dispatcher(_guard_plugin(tmp_path, seen))
    try:
        with TestClient(app) as client:
            # bypass: only the hook stands between the agent and the file.
            sid = workspace_session(client, root, approval_mode="bypass")
            assistant = complete_turn(client, sid, "overwrite the protected file")
            diffs = client.get(f"/v1/sessions/{sid}/diffs").json()["diffs"]
    finally:
        install_global_dispatcher(None)

    assert target.read_text(encoding="utf-8") == "original\n"
    events = [json.loads(line) for line in seen.read_text(encoding="utf-8").splitlines()]
    assert events == [
        {
            "tool_name": "Write",
            "tool_input": {"file_path": str(target), "content": "clobbered\n"},
        },
        {
            "tool_name": "Edit",
            "tool_input": {
                "file_path": str(target),
                "old_string": "original",
                "new_string": "edited",
            },
        },
    ]
    # The hook's reason is what the agent reads as the refused call's error.
    assert len(agent.results) == 2
    for refused in agent.results:
        assert isinstance(refused, PermissionError)
        assert "WTF-P: writes to protected/ are blocked" in str(refused)
    # And it reaches the UI on the audited permission row; nothing was recorded.
    denied = [r for r in app.state.permissions.values() if r.get("reason") == "hook_deny"]
    assert {r["tool_call"]["tool_name"] for r in denied} == {"fs_write_file", "fs_edit_file"}
    assert diffs == []
    assert not [p for p in assistant["parts"] if p["type"] == "file_diff"]


def test_hook_deny_reaches_the_gate_decision(tmp_path: Path, root: Path) -> None:
    seen = tmp_path / "seen.jsonl"
    app = build_app(sessions_path=tmp_path / "s.json")
    install_global_dispatcher(_guard_plugin(tmp_path, seen))
    try:
        with TestClient(app) as client:
            sid = workspace_session(client, root, approval_mode="bypass")
            with _gact_app_context(app), _tool_session_context(sid):
                decision = _make_permission_gate(app)(
                    "fs_write_file", {"filepath": str(root / "x"), "content": "y"}
                )
    finally:
        install_global_dispatcher(None)
    assert isinstance(decision, DenyDecision)
    assert decision.deny_message == "WTF-P: writes to protected/ are blocked"


# --------------------------------------------------------------------------- #
# The record of changes, and Undo
# --------------------------------------------------------------------------- #


def _run_writes(tmp_path: Path, root: Path, calls: list[tuple[str, dict[str, Any]]]):
    agent = WritingAgent()
    agent.calls = calls
    app = build_app(sessions_path=tmp_path / "s.json", agent=agent)
    client = TestClient(app)
    client.__enter__()
    sid = workspace_session(client, root, approval_mode="auto-edits")
    assistant = complete_turn(client, sid, "change the files")
    return app, client, sid, assistant, agent


def test_writes_are_recorded_as_applied_diffs_and_undo_restores(tmp_path: Path, root: Path) -> None:
    edited = root / "notes.md"
    edited.write_text("alpha\nbeta\n", encoding="utf-8")
    created = root / "new" / "made.txt"
    app, client, sid, assistant, agent = _run_writes(
        tmp_path,
        root,
        [
            ("fs_edit_file", {"filepath": str(edited), "old_string": "beta", "new_string": "B"}),
            ("fs_write_file", {"filepath": str(created), "content": "fresh\n"}),
        ],
    )
    try:
        assert edited.read_text(encoding="utf-8") == "alpha\nB\n"
        assert created.read_text(encoding="utf-8") == "fresh\n"
        parts = [p for p in assistant["parts"] if p["type"] == "file_diff"]
        assert [(p["path"], p["status"]) for p in parts] == [
            (str(edited), "applied"),
            (str(created), "applied"),
        ]
        diffs = client.get(f"/v1/sessions/{sid}/diffs").json()["diffs"]
        assert [d["path"] for d in diffs] == [str(edited), str(created)]
        assert all(d["status"] == "applied" and d["undo_available"] for d in diffs)
        assert [d["created"] for d in diffs] == [False, True]
        assert {d["message_id"] for d in diffs} == {assistant["id"]}
        assert {d["part_id"] for d in diffs} == {p["id"] for p in parts}
        assert "+B" in diffs[0]["unified_diff"]
        # The previous text never rides a wire or the tool-call ledger.
        assert "previous_content" not in json.dumps(diffs)
        assert "_clio_previous_content" not in json.dumps(assistant)

        undo_edit = client.post(f"/v1/sessions/{sid}/diffs/revert", json={"id": diffs[0]["id"]})
        assert undo_edit.status_code == 200, undo_edit.text
        assert undo_edit.json()["reverted"]["status"] == "reverted"
        assert edited.read_text(encoding="utf-8") == "alpha\nbeta\n"

        undo_create = client.post(
            f"/v1/sessions/{sid}/diffs/revert", json={"id": diffs[1]["part_id"]}
        )
        assert undo_create.status_code == 200, undo_create.text
        assert not created.exists()

        again = client.post(f"/v1/sessions/{sid}/diffs/revert", json={"id": diffs[0]["id"]})
        assert again.status_code == 409
        assert again.json()["error"]["error"] == "already_reverted"

        reverted = [
            e for e in app.state.bus._history.get(sid, []) if e.type == "file.diff.reverted"
        ]
        assert len(reverted) == 2
        audit = [
            r for r in app.state.permissions.values() if r.get("reason") == "user_requested_revert"
        ]
        assert len(audit) == 2
    finally:
        client.__exit__(None, None, None)


def test_undo_refuses_when_the_file_changed_since_the_write(tmp_path: Path, root: Path) -> None:
    target = root / "data.txt"
    target.write_text("one\n", encoding="utf-8")
    app, client, sid, _assistant, _agent = _run_writes(
        tmp_path, root, [("fs_write_file", {"filepath": str(target), "content": "two\n"})]
    )
    try:
        [row] = client.get(f"/v1/sessions/{sid}/diffs").json()["diffs"]
        target.write_text("three\n", encoding="utf-8")  # the person edits it afterwards

        resp = client.post(f"/v1/sessions/{sid}/diffs/revert", json={"id": row["id"]})

        assert resp.status_code == 409
        error = resp.json()["error"]
        assert error["error"] == "file_changed_since_write"
        assert error["details"]["sha256"] == _sha(b"three\n")
        assert error["details"]["expected_sha256"] == _sha(b"two\n")
        assert target.read_text(encoding="utf-8") == "three\n"
    finally:
        client.__exit__(None, None, None)


def test_undo_unavailable_when_the_previous_content_was_too_large(
    tmp_path: Path, root: Path
) -> None:
    target = root / "big.txt"
    target.write_text("x" * (UNDO_CAPTURE_MAX_BYTES + 1), encoding="utf-8")
    app, client, sid, _assistant, _agent = _run_writes(
        tmp_path, root, [("fs_write_file", {"filepath": str(target), "content": "small\n"})]
    )
    try:
        [row] = client.get(f"/v1/sessions/{sid}/diffs").json()["diffs"]
        assert row["undo_available"] is False
        assert row["undo_unavailable_reason"] == "previous_content_too_large"

        resp = client.post(f"/v1/sessions/{sid}/diffs/revert", json={"id": row["id"]})

        assert resp.status_code == 409
        error = resp.json()["error"]
        assert error["error"] == "undo_unavailable"
        assert error["details"]["reason"] == "previous_content_too_large"
        assert target.read_text(encoding="utf-8") == "small\n"
    finally:
        client.__exit__(None, None, None)


def test_undo_of_an_unknown_change_is_not_found(tmp_path: Path) -> None:
    with TestClient(build_app(sessions_path=tmp_path / "s.json")) as client:
        sid = client.post("/v1/sessions", json={"title": "t"}).json()["id"]
        resp = client.post(f"/v1/sessions/{sid}/diffs/revert", json={"id": "chg_nope"})
        missing = client.post(f"/v1/sessions/{sid}/diffs/revert", json={})
        unknown = client.post("/v1/sessions/sess_nope/diffs/revert", json={"id": "x"})
    assert resp.status_code == 404
    assert resp.json()["error"]["error"] == "file_change_not_found"
    assert missing.status_code == 422
    assert unknown.status_code == 404


def test_apply_and_reject_routes_are_gone(tmp_path: Path) -> None:
    with TestClient(build_app(sessions_path=tmp_path / "s.json")) as client:
        sid = client.post("/v1/sessions", json={"title": "t"}).json()["id"]
        for route in ("apply", "reject"):
            resp = client.post(f"/v1/sessions/{sid}/diffs/{route}", json={})
            assert resp.status_code in {404, 405}, route


def test_undo_restores_inside_the_workspace_only(tmp_path: Path, root: Path) -> None:
    """A row pointing outside the session's workspace root is refused typed."""

    with TestClient(build_app(sessions_path=tmp_path / "s.json")) as client:
        sid = workspace_session(client, root, approval_mode="ask")
        outside = tmp_path / "outside.txt"
        outside.write_text("now\n", encoding="utf-8")
        client.app.state.file_changes[sid] = [
            {
                "id": "chg_out",
                "path": str(outside),
                "status": "applied",
                "created": False,
                "previous_content": "before\n",
                "sha256_after": _sha(b"now\n"),
            }
        ]
        with tool_workspace_context(str(root)):
            resp = client.post(f"/v1/sessions/{sid}/diffs/revert", json={"id": "chg_out"})
    assert resp.status_code == 403
    assert resp.json()["error"]["error"] == "path_outside_workspace"
    assert outside.read_text(encoding="utf-8") == "now\n"
