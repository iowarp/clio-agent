"""Stop reaches a plain MCP call: it runs as "running" and its cancel reaches the server.

Found live (KiCad ledger KN29): "Route this board.", then Stop and "Stop everything" while
FreeRouting ran. The reply stopped, the dialog had listed the call as "Queued", and
FreeRouting ran another 167 s to its end, because cancelling the call's handle found no
call (a plain call, no MCP task, was held by nothing) and answered "Original task
transport is unavailable".
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import pytest
from fastmcp import Client
from fastmcp.client.transports import StdioTransport

from clio_agent.arc.memory import ARCMemory
from clio_agent.gact import context, external_plain_calls
from clio_agent.gact.external_tasks import ExternalTasks
from clio_agent.gact.workspaces import WorkspaceStore
from clio_agent.tools.task_call_context import TASK_CALL, TaskCall
from tests.test_arc.test_conversation_owner_contracts import create_environment

SERVER = """
import asyncio
import sys
from pathlib import Path
from fastmcp import FastMCP

marks = Path(sys.argv[1])
server = FastMCP("slow-plain")

@server.tool
async def route() -> str:
    (marks / "started").write_text("1")
    try:
        await asyncio.Event().wait()  # works until it is told to stop
    except asyncio.CancelledError:
        (marks / "cancelled").write_text("1")
        raise
    return "routed"

server.run(transport="stdio", show_banner=False)
"""


@pytest.mark.asyncio
@pytest.mark.timeout(120)  # A real stdio server must start, be cancelled and retire.
async def test_a_running_plain_call_is_running_and_its_cancel_reaches_the_server(
    arc: ARCMemory, tmp_path: Path
) -> None:
    app, conversations, root = await create_environment(arc, tmp_path)
    history = conversations.history
    app.state.task_recovery = conversations.recovery

    def setup_workspace() -> None:
        app.state.workspaces = WorkspaceStore(path=tmp_path / "workspaces.json")
        workspace = app.state.workspaces.create(name="Plain call", root_path=str(tmp_path))
        session = app.state.sessions.get(root.owner.session_id)
        assert session is not None
        session.workspace_id = workspace.id
        app.state.sessions._persist(session)

    await asyncio.to_thread(setup_workspace)
    external = ExternalTasks(app)
    script, marks = tmp_path / "slow_server.py", tmp_path / "marks"
    script.write_text(SERVER, encoding="utf-8")
    marks.mkdir()
    (tmp_path / "owned-processes.json").write_text(
        json.dumps({"purpose": "plain call cancel", "script": str(script)}), encoding="utf-8"
    )

    async def call() -> Any:
        tokens = (context.set_app(app), context.set_session_id(root.owner.session_id))
        invocation = TASK_CALL.set(TaskCall(invocation_id="plain-route"))
        try:
            return await external.call(
                "slow-plain",
                Client(StdioTransport(sys.executable, ["-B", str(script), str(marks)])),
                "route",
                {},
                auth_scope="trusted-test-scope",
            )
        finally:
            TASK_CALL.reset(invocation)
            for token in reversed(tokens):
                context.reset(token)

    running = asyncio.create_task(call())
    try:
        while not (marks / "started").exists():
            await asyncio.sleep(0.05)
        handles = await asyncio.to_thread(history.task_handles, kind="MCP", active_only=True)
        assert len(handles) == 1
        (handle,) = handles

        async def recorded_running() -> Any:
            while True:
                task = await asyncio.to_thread(history.task_snapshot, handle)
                if task is not None and task.status == "running":
                    return task
                await asyncio.sleep(0.05)

        task = await recorded_running()
        assert task.activity == "executing", "a sent call is running, not 'Queued'"
        assert external_plain_calls.in_flight(handle)

        acknowledgement = await external.cancel(handle)

        assert acknowledgement["cancellation_requested"] is True
        assert acknowledgement["status"] == "cancelled"
        with pytest.raises(asyncio.CancelledError):
            await running
        while not (marks / "cancelled").exists():  # notifications/cancelled reached the tool
            await asyncio.sleep(0.05)
        ended = await asyncio.to_thread(history.task_snapshot, handle)
        assert ended is not None and ended.status == "cancelled"
        assert ended.raw_status == external_plain_calls.PLAIN_CALL_CANCELLED
        assert ended.settlement == "unknown"
        assert not external_plain_calls.in_flight(handle)
    finally:
        running.cancel()
        await asyncio.gather(running, return_exceptions=True)
        await external.close()
        await conversations.shutdown()
        await conversations.recovery.drain()
