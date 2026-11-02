"""A read-only MCP call is sent before its admission is written; other calls stay write-ahead.

Owner decision (2026-11-01): read-only calls need no durable record before they are
sent; calls that can change something keep it. Measured on P7 the write-ahead
admission serialized every call through the single history writer (submissions
46 ms apart while 9 were allowed in flight). The read-only call's submission and
snapshot are written with its result, in one history-writer operation.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from fastmcp import Client, FastMCP
from mcp.types import ToolAnnotations

from clio_agent.arc.memory import ARCMemory
from clio_agent.gact import context
from clio_agent.gact.external_read_only import skips_write_ahead
from clio_agent.gact.external_tasks import ExternalTasks
from clio_agent.gact.tool_execution_projection import own_execution_tasks
from clio_agent.gact.workspaces import WorkspaceStore
from clio_agent.tools.task_call_context import TASK_CALL, TaskCall
from tests.test_arc.test_conversation_owner_contracts import create_environment


def _server(seen: dict[str, set[str]], history: Any) -> FastMCP:
    """``read`` is declared read-only, ``write`` is not; each notes the MCP tasks
    recorded while it runs."""
    server = FastMCP("records")

    async def recorded() -> set[str]:
        return await asyncio.to_thread(history.task_handles, kind="MCP")

    @server.tool(annotations=ToolAnnotations(readOnlyHint=True))
    async def read(record_id: str) -> dict[str, str]:
        seen["read"] = await recorded()
        return {"record_id": record_id}

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True))
    async def write(record_id: str) -> dict[str, str]:
        seen["write"] = await recorded()
        return {"record_id": record_id}

    return server


async def _environment(arc: ARCMemory, tmp_path: Path) -> tuple[Any, Any]:
    app, conversations, root = await create_environment(arc, tmp_path)
    app.state.task_recovery = conversations.recovery
    app.state.conversations = conversations

    def setup_workspace() -> None:
        app.state.workspaces = WorkspaceStore(path=tmp_path / "workspaces.json")
        workspace = app.state.workspaces.create(name="records", root_path=str(tmp_path))
        session = app.state.sessions.get(root.owner.session_id)
        assert session is not None
        session.workspace_id = workspace.id
        app.state.sessions._persist(session)

    await asyncio.to_thread(setup_workspace)
    return app, root


async def _call(app: Any, root: Any, external: ExternalTasks, client: Client, tool: str) -> Any:
    tokens = (context.set_app(app), context.set_session_id(root.owner.session_id))
    invocation = TASK_CALL.set(TaskCall(invocation_id="inv-" + tool))
    try:
        return await external.call(
            "records", client, tool, {"record_id": "REC-1"}, auth_scope="trusted-test-scope"
        )
    finally:
        TASK_CALL.reset(invocation)
        for token in reversed(tokens):
            context.reset(token)


@pytest.mark.asyncio
@pytest.mark.timeout(60)  # Two in-memory MCP sessions open and close.
@pytest.mark.parametrize("mode", ["non_read_only", "all"])
async def test_a_read_only_call_is_admitted_with_its_result(
    arc: ARCMemory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    """SABOTAGE: make ``skips_write_ahead`` return False and the read sees its own task."""
    monkeypatch.setenv("CLIO_TOOLS_WRITE_AHEAD", mode)
    app, root = await _environment(arc, tmp_path)
    history = app.state.task_history
    seen: dict[str, set[str]] = {}
    snapshots: list[str] = []
    original = history.record_snapshot

    def counted(snapshot: Any, **kwargs: Any) -> Any:
        snapshots.append(snapshot.tool)
        return original(snapshot, **kwargs)

    monkeypatch.setattr(history, "record_snapshot", counted)
    external = ExternalTasks(app)
    client = Client(_server(seen, history))
    try:
        read = await _call(app, root, external, client, "read")
        write = await _call(app, root, external, client, "write")
    finally:
        await external.close()
        await app.state.task_recovery.drain()

    assert not read.is_error and not write.is_error
    assert external._lazy == {}

    def settled() -> dict[str, Any]:
        handles = history.task_handles(session_id=root.owner.session_id)
        tasks = [history.task_snapshot(handle) for handle in handles]
        return {task.tool: task for task in tasks if task.kind == "MCP"}

    tasks = await asyncio.to_thread(settled)
    reader = tasks["read"]
    # A write is confirmed before it is sent: it sees its own task.
    assert tasks["write"].handle in seen["write"]
    # A read is sent first under the default; with "all" it is write-ahead too.
    assert (reader.handle in seen["read"]) is (mode == "all")
    # Sent first, its one snapshot is the settled one; write-ahead records running too.
    # SABOTAGE: drop ``lazy.settled`` in ``_call`` and the default records two.
    assert snapshots.count("read") == (1 if mode == "non_read_only" else 2)
    # Recorded afterwards exactly as a write-ahead call is: submission, then result.
    assert reader.status == "completed" and reader.raw_status == "inline"
    assert reader.submission is not None and reader.result is not None
    submitted = await asyncio.to_thread(history.read_content, reader.submission)
    assert submitted["tool"] == "read" and submitted["arguments"] == {"record_id": "REC-1"}

    # A call's own tasks come from the invocation index, not a scan of every session
    # task (P7: 2,000 scanned per completion). SABOTAGE: scan session_handles again.
    def scanned(*_args: Any) -> set[str]:
        raise AssertionError("own_execution_tasks scanned every session task")

    monkeypatch.setattr(history, "session_handles", scanned)
    sid = root.owner.session_id
    found = await asyncio.to_thread(history.invocation_handles, sid, {"inv-read", "inv-write"})
    assert found == {reader.handle, tasks["write"].handle}
    # Inline calls have no backend task to join; found without listing the session.
    assert await asyncio.to_thread(own_execution_tasks, app, sid, {"inv-read"}) == {}


def test_only_declared_read_only_tools_without_task_support_skip_write_ahead() -> None:
    from types import SimpleNamespace

    read_only = ToolAnnotations(readOnlyHint=True)
    assert skips_write_ahead(SimpleNamespace(annotations=read_only, execution=None))
    assert not skips_write_ahead(SimpleNamespace(annotations=None, execution=None))
    assert not skips_write_ahead(
        SimpleNamespace(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=True))
    )
    tasked = SimpleNamespace(taskSupport="optional")
    assert not skips_write_ahead(SimpleNamespace(annotations=read_only, execution=tasked))
