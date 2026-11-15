"""A command outside the sandbox runs only after a person allows that exact command."""

from __future__ import annotations

import asyncio
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from clio_agent import conf
from clio_agent.gact.app import build_app
from clio_agent.gact.permission_gate import _make_permission_gate
from clio_agent.gact.runtime.globals import _gact_app_context, _tool_session_context
from clio_agent.runtime import sandbox_diagnosis as sd
from clio_agent.runtime import sandbox_outside


def _workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, approval_mode: str
) -> tuple[Any, TestClient, Path, str]:
    root = tmp_path / "Workspace"
    root.mkdir()
    monkeypatch.setenv("CLIO_ALLOWED_ROOTS", str(root))
    monkeypatch.setitem(conf.store().data["tools"]["file_policy"], "allowed_roots", [str(root)])
    app = build_app(sessions_path=tmp_path / "sessions.json")
    client = TestClient(app)
    wid = client.post("/v1/workspaces", json={"name": "Install", "root_path": str(root)}).json()[
        "id"
    ]
    sid = client.post("/v1/sessions", json={"workspace_id": wid}).json()["id"]
    assert client.patch(f"/v1/sessions/{sid}", json={"approval_mode": approval_mode}).is_success
    return app, client, root, sid


def _pending(app: Any, future: Any) -> dict[str, Any]:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if future.done():
            future.result()
        rows = [row for row in app.state.permissions.values() if row["status"] == "pending"]
        if rows:
            return rows[0]
        threading.Event().wait(0.01)
    raise AssertionError("No permission was asked")


@pytest.mark.parametrize("action", ["allow", "deny"])
def test_the_person_is_asked_even_in_bypass_and_the_allow_is_for_that_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, action: str
) -> None:
    app, client, root, sid = _workspace(tmp_path, monkeypatch, "bypass")
    command = "apt-get install -y some-tool"
    args = {
        "command": command,
        "cwd": str(root),
        "outside_sandbox": True,
        "access_reason": "Install the tool the person chose; the sandbox blocks apt.",
    }

    def call() -> Any:
        with _gact_app_context(app), _tool_session_context(sid):
            return _make_permission_gate(app)("shell_bash", args)

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(call)
        row = _pending(app, future)
        assert row["summary"] == "Run this command outside CLIO's sandbox?"
        assert row["tool_call"]["input"]["access_reason"].endswith("the sandbox blocks apt.")
        assert row["tool_call"]["input"]["command"] == command
        assert row["actions"] == ["allow", "deny"]  # once, never a standing rule
        refused = client.post(f"/v1/permissions/{row['id']}", json={"action": "allow_session"})
        assert refused.status_code == 422
        assert client.post(f"/v1/permissions/{row['id']}", json={"action": action}).is_success
        decision = future.result(timeout=5)
    if action == "deny":
        assert decision != "allow"
        assert not sandbox_outside.take(command)
    else:
        assert decision == "allow"
        assert not sandbox_outside.take("apt-get install -y another-tool")
        assert sandbox_outside.take(command)
        assert not sandbox_outside.take(command)  # used once


def test_the_shell_refuses_without_an_allow_and_runs_unfenced_with_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from clio_agent.tools.servers.shell_server import execute_shell

    class Progress:
        async def report_progress(self, *args: Any, **kwargs: Any) -> None:
            return None

    monkeypatch.setenv("CLIO_ALLOWED_ROOTS", str(tmp_path))
    command = "echo outside"
    refused = asyncio.run(execute_shell(Progress(), command, str(tmp_path), outside_sandbox=True))
    assert refused["error"]["code"] == sandbox_outside.REASON_NOT_APPROVED
    sandbox_outside.approve(command)
    ran = asyncio.run(execute_shell(Progress(), command, str(tmp_path), outside_sandbox=True))
    assert ran.get("exit_code") == 0 and "outside" in ran.get("stdout", "")


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX account check")
def test_a_denial_inside_the_territory_the_account_may_write_is_privilege_denied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sd, "_server_is_root", lambda: True)  # a server running as root
    root = tmp_path / "granted"
    target = root / "partial" / "lists.part"
    target.parent.mkdir(parents=True)
    target.write_text("x")
    assert os.access(target, os.W_OK)
    diagnosis = sd.diagnose_boundary(
        exit_code=100,
        stderr=f"W: Problem unlinking the file {target} - PrepareFiles (13: Permission denied)",
        command="apt-get update",
        cwd=tmp_path,
        write_roots=[root],
        mechanism="codex",
        platform="linux",
    )
    assert diagnosis is not None and diagnosis.kind == sd.KIND_PRIVILEGE_DENIED
    assert str(target) in diagnosis.paths
    assert "outside_sandbox=true" in diagnosis.message
    assert sd.BoundaryDiagnosis.from_payload(diagnosis.to_payload()) == diagnosis
