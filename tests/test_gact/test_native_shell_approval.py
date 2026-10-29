"""Bridge approval has exact, one-shot custody across the native Shell handoff."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastmcp import FastMCP

from clio_agent.arc.memory import ARCMemory
from clio_agent.gact.builtin_boundary import BuiltinBoundary
from clio_agent.gact.permission_gate import _policy_action_for_tool
from clio_agent.gact.planning import Playbook, PlaybookStep, record_playbook
from clio_agent.gact.tool_observer import _OBSERVER_CALL_IDS
from clio_agent.tools import execution
from clio_agent.tools.execution import SyncMCPToolExecutor, ToolRuntimeHooks
from clio_agent.tools.task_call_context import capture_approval
from clio_agent.tools.tool_hooks import InterceptDecision
from tests._config_layer import set_config
from tests.test_arc.conftest import core_arc as core_arc
from tests.test_arc.test_conversation_owner_contracts import create_environment
from tests.test_gact.task_records import active_turn_fixture
from tests.test_tools.test_execution import FakeClient


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        "none",
        "command",
        "policy",
        "policy_session",
        "policy_workspace",
        "playbook",
        "spotter",
        "folder",
        "deny",
    ],
)
async def test_bridge_native_shell_uses_one_exact_approval(
    arc: ARCMemory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    """Exercise actual sync-to-async dispatch and native admission without an OS process."""
    app, owner, root = await create_environment(arc, tmp_path)
    set_config("tools.file_policy.allowed_roots", [str(tmp_path)])
    sid = root.owner.session_id
    app.state.workspaces = SimpleNamespace(
        get=lambda _wid: SimpleNamespace(root_path=str(tmp_path))
    )
    app.state.mcp_app_loop = asyncio.get_running_loop()
    app.state.permission_policies = []
    if change == "spotter":
        app.state.sessions.update(sid, approval_mode="spotter-ai")
        monkeypatch.setattr(
            "clio_agent.gact.spotter_permission.wait_for_spotter_clearance",
            lambda _app, _sid: "spotter_watcher_check_failed",
        )
        monkeypatch.setattr(
            "clio_agent.gact.permission_gate._record_resolved_permission",
            lambda *_args, **_kwargs: None,
        )
    shell = FastMCP("approval-schema")

    @shell.tool
    def bash(
        command: str, cwd: str, background: bool = False, access_paths: list[str] | None = None
    ) -> dict[str, Any]:
        """Declare the actual schema; this test stops at native admission."""
        pytest.fail("The schema fixture must never execute a process")

    outer: list[dict[str, Any]] = []
    inner: list[dict[str, Any]] = []
    submissions: list[tuple[str, str, dict[str, Any], dict[str, Any]]] = []

    def outer_gate(name: str, arguments: dict[str, Any]) -> str:
        outer.append({"name": name, "arguments": dict(arguments)})
        return "deny" if change == "deny" else "allow"

    def native_gate(name: str, arguments: dict[str, Any], metadata: Any) -> str:
        inner.append({"name": name, "arguments": dict(arguments), "metadata": metadata})
        if change.startswith("policy_") or change == "playbook":
            return _policy_action_for_tool(
                app,
                session_id=sid,
                session=app.state.sessions.get(sid),
                tool_name=name,
                args=arguments,
                mode="edit",
            )
        return "deny" if change in {"policy", "folder"} else "allow"

    async def submit(
        family: str, tool: str, arguments: dict[str, Any], **metadata: Any
    ) -> dict[str, Any]:
        submissions.append((family, tool, dict(arguments), metadata))
        return {"handle": "accepted-shell", "pending": True}

    app.state.native_tools = SimpleNamespace(
        roots={"shell": SimpleNamespace(server=shell)}, submit=submit
    )
    app.state.make_permission_gate = lambda: native_gate
    app.state.builtin_boundary = BuiltinBoundary(app, lambda _saved: None)

    def observer(name: str, arguments: Any, phase: str, *details: Any) -> None:
        if phase == "started":
            _OBSERVER_CALL_IDS.set("approved-shell-invocation")
            if change.startswith("policy"):
                scope = change.removeprefix("policy_") if "_" in change else "global"
                app.state.permission_policies.append(
                    {
                        "scope": scope,
                        "scope_id": "",
                        "action": "deny",
                        "kind": "tool",
                        "tool_name_pattern": "shell_bash",
                    }
                )
            if change == "playbook":
                record_playbook(
                    app,
                    sid,
                    Playbook(
                        name="Narrowed",
                        steps=(PlaybookStep(name="Read only", tools_allowed=("fs_read_file",)),),
                    ),
                )

    def intercept(name: str, arguments: Any) -> InterceptDecision | None:
        if change == "command":
            return InterceptDecision(
                kind="modify", modified_args={**arguments, "command": "changed"}
            )
        return None

    monkeypatch.setattr(
        execution, "current_tool_runtime", lambda: ToolRuntimeHooks(tool_interceptor=intercept)
    )

    def invoke() -> str:
        with active_turn_fixture(app, sid):
            token = _OBSERVER_CALL_IDS.set("")
            try:
                with SyncMCPToolExecutor(
                    object(),
                    timeout=5,
                    client_factory=lambda _: FakeClient(),
                    preloaded_tools={
                        "shell_bash": SimpleNamespace(
                            name="shell_bash",
                            inputSchema={"properties": {}},
                            annotations={"readOnlyHint": False},
                        )
                    },
                    permission_gate=outer_gate,
                    tool_observer=observer,
                ) as bridge:
                    arguments: dict[str, Any] = {
                        "command": "original",
                        "cwd": str(tmp_path),
                        "_clio_task_description": "Reviewed command",
                    }
                    if change == "folder":
                        arguments["access_paths"] = [str(tmp_path.parent / "external")]
                    return bridge.call_tool("shell_bash", arguments)
            finally:
                _OBSERVER_CALL_IDS.reset(token)

    try:
        if change in {"deny", "folder", "spotter", "playbook"} or change.startswith("policy"):
            with pytest.raises(PermissionError):
                await asyncio.to_thread(invoke)
            assert submissions == []
        else:
            assert "accepted-shell" in await asyncio.to_thread(invoke)
            assert len(submissions) == 1
            family, tool, arguments, metadata = submissions[0]
            assert (family, tool) == ("shell", "bash")
            assert arguments["command"] == ("changed" if change == "command" else "original")
            assert "_clio_task_description" not in arguments
            assert metadata["owner"].assignment_id == root.handle
            assert metadata["invocation_id"] == "approved-shell-invocation"
            assert metadata["workspace"] == str(tmp_path)
        assert len(outer) == 1
        assert len(inner) == (
            1 if change in {"command", "folder", "playbook"} or change.startswith("policy") else 0
        )
        if change == "none":
            assert app.state.permission_policies == []
    finally:
        await owner.recovery.drain()
        await owner.history.io.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        "none",
        "replay",
        "command",
        "cwd",
        "session",
        "workspace",
        "root",
        "assignment",
        "invocation",
        "tool",
        "policy",
        "mode",
        "profile",
        "app",
        "rebind",
    ],
)
async def test_native_approval_cannot_escape_its_invocation(
    arc: ARCMemory, tmp_path: Path, change: str
) -> None:
    """An approval is unusable outside its original exact custody and posture."""
    app, owner, root = await create_environment(arc, tmp_path)
    sid = root.owner.session_id
    workspace = SimpleNamespace(root_path=str(tmp_path))
    app.state.workspaces = SimpleNamespace(get=lambda _wid: workspace)
    app.state.permission_policies = []
    arguments = {"command": "reviewed", "cwd": str(tmp_path), "access_paths": [str(tmp_path)]}
    try:
        with active_turn_fixture(app, sid):
            receipt = capture_approval("shell_bash", arguments)
        assert receipt is not None and receipt.bind("original-invocation") is receipt
        target, session_id, assignment_id, invocation_id, tool = (
            app,
            sid,
            root.handle,
            "original-invocation",
            "shell_bash",
        )
        if change in {"command", "cwd"}:
            arguments[change] = "different"
        elif change == "session":
            session_id = "other-session"
        elif change == "workspace":
            session = app.state.sessions.get(sid)
            assert session is not None
            session.workspace_id = "other-workspace"
        elif change == "root":
            workspace.root_path = str(tmp_path / "other")
        elif change == "assignment":
            assignment_id = "other-assignment"
        elif change == "invocation":
            invocation_id = "other-invocation"
        elif change == "tool":
            tool = "other-tool"
        elif change == "policy":
            app.state.permission_policies.append({"scope": "global", "action": "deny"})
        elif change == "mode":
            app.state.sessions.update(sid, mode="plan")
        elif change == "profile":
            app.state.sessions.update(sid, approval_profile="read-only-side")
        elif change == "app":
            target = SimpleNamespace(state=app.state)
        elif change == "rebind":
            assert receipt.bind("another-invocation") is None
        result = receipt.consume(target, session_id, assignment_id, invocation_id, tool, arguments)
        assert result is (change in {"none", "replay", "rebind"})
        assert (
            receipt.consume(
                target, sid, root.handle, "original-invocation", "shell_bash", arguments
            )
            is False
        )
    finally:
        await owner.recovery.drain()
        await owner.history.io.close()
