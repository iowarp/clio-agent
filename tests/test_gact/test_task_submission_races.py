"""Real process acceptance and durable admission races at the application boundary."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from clio_agent.gact.agent_tasks import AgentTaskRegistry
from clio_agent.gact.events import EventBus
from clio_agent.gact.mcp_task_store import SessionMetadataTaskStore
from clio_agent.gact.sessions import SessionStore
from clio_agent.gact.task_projection import resolve_task, task_views
from clio_agent.gact.task_shell_owner import spawn_owned_shell
from clio_agent.gact.task_subagent_owner import close_subtree_admission
from clio_agent.gact.task_supervisor import task_supervisor
from clio_agent.runtime.task_process_tree import task_spawn_options
from clio_agent.tools.servers.shell_output import StreamCapture
from clio_agent.tools.task_call_context import require_admission


def application(tmp_path: Path) -> tuple[Any, str]:
    """Use the actual persisted session and task owners."""
    sessions = SessionStore(path=tmp_path / "sessions.json")
    sid = sessions.create(workspace_id="ws", title="parent").id
    return SimpleNamespace(
        state=SimpleNamespace(
            sessions=sessions,
            mcp_task_store=SessionMetadataTaskStore(sessions),
            agent_task_registry=AgentTaskRegistry(),
            bus=EventBus(),
            cancel_flags=set(),
        )
    ), sid


def test_stop_during_actual_shell_spawn_finishes_durable_acceptance(tmp_path: Path) -> None:
    app, sid = application(tmp_path)

    async def exercise() -> None:
        app.state.mcp_app_loop = asyncio.get_running_loop()
        spawned, release = asyncio.Event(), asyncio.Event()
        supervisor = task_supervisor(app)

        async def spawn() -> asyncio.subprocess.Process:
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-c",
                "import time; print('accepted once',flush=True); time.sleep(120)",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                **task_spawn_options({}),
            )
            spawned.set()
            await release.wait()
            return process

        captures = [
            StreamCapture(name, inline_limit=4096, spill_path=tmp_path / f"{name}.log")
            for name in ("stdout", "stderr")
        ]
        waiter = asyncio.create_task(
            spawn_owned_shell(
                app,
                sid,
                spawn(),
                captures,
                command="owned acceptance race",
                cwd=tmp_path,
                spill_root=tmp_path,
                timeout=0,
                invocation_id="race-call",
            )
        )
        try:
            await asyncio.wait_for(spawned.wait(), 5)
            waiter.cancel()
            await asyncio.sleep(0)
            assert not waiter.done()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(waiter, 5)
            rows = task_views(app, sid)
            assert len(rows) == 1
            handle = rows[0]["handle"]
            assert handle.startswith("task_")
            assert rows[0]["invocation_id"] == "race-call"
            assert rows[0]["effective_status"] == "running"
            assert await asyncio.to_thread(supervisor.request_cancel, rows[0])
            driver = supervisor.drivers.get(handle)
            if driver is not None:
                await asyncio.wait_for(asyncio.shield(driver), 5)
            assert resolve_task(app, sid, handle)["effective_status"] == "cancelled"
        finally:
            release.set()
            await supervisor.shutdown()

    asyncio.run(exercise())


def test_subtree_admission_closure_survives_restart_and_includes_forks(tmp_path: Path) -> None:
    app, sid = application(tmp_path)
    child = app.state.sessions.create(workspace_id="ws", title="child", parent_session_id=sid)
    fork = app.state.sessions.create(workspace_id="ws", title="fork", parent_session_id=child.id)
    close_subtree_admission(app, child.id)
    app.state.task_admission_closed = set()
    app.state.sessions = SessionStore(path=tmp_path / "sessions.json")
    require_admission(app, sid)
    for closed_sid in (child.id, fork.id):
        with pytest.raises(RuntimeError, match="admission is closed"):
            require_admission(app, closed_sid)


def test_stop_before_mcp_receipt_retains_actual_backend_acceptance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stop releases a waiter before durable acknowledgement without replaying tools/call."""
    from datetime import timedelta

    from fastmcp import FastMCP
    from fastmcp.utilities.tasks import TaskConfig
    from fastmcp_tasks.extension import TasksExtension

    from clio_agent.gact import context
    from clio_agent.tools.mcp_executor import AsyncMCPToolExecutor
    from clio_agent.tools.mcp_task_extension import ClioTasksClientExtension
    from clio_agent.tools.mcp_task_records import set_task_record_store

    app, sid = application(tmp_path)
    server = FastMCP("actual-acceptance-race")
    server.add_extension(TasksExtension())
    calls: list[str] = []

    async def exercise() -> None:
        app.state.mcp_app_loop = asyncio.get_running_loop()
        reached, release, finish = asyncio.Event(), asyncio.Event(), asyncio.Event()

        @server.tool(task=TaskConfig(mode="required", poll_interval=timedelta(milliseconds=50)))
        async def actual_work() -> str:
            calls.append("accepted once")
            await finish.wait()
            return "actual backend completed"

        original = ClioTasksClientExtension._make_durable

        async def gated(extension: Any, *args: Any, **kwargs: Any) -> None:
            reached.set()
            await release.wait()
            await original(extension, *args, **kwargs)

        monkeypatch.setattr(ClioTasksClientExtension, "_make_durable", gated)
        token = context.set_app(app)
        session_token = context.set_session_id(sid)
        tool_session_token = context.set_tool_session_id(sid)
        set_task_record_store(app.state.mcp_task_store)
        executor = AsyncMCPToolExecutor(server)
        try:
            await executor.start()
            waiter = asyncio.create_task(executor.call_tool_result("actual_work", {}))
            await asyncio.wait_for(reached.wait(), 5)
            waiter.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(waiter, 1)
            await executor.aclose()
            assert not executor._closed and executor._pending_task_submissions
            release.set()
            for _ in range(100):
                if task_views(app, sid):
                    break
                await asyncio.sleep(0.02)
            rows = task_views(app, sid)
            assert len(rows) == 1 and rows[0]["effective_status"] == "running"
            handle = rows[0]["handle"]
            driver = task_supervisor(app).drivers[handle]
            finish.set()
            await asyncio.wait_for(asyncio.shield(driver), 5)
            assert calls == ["accepted once"]
            assert resolve_task(app, sid, handle)["effective_status"] == "completed"
            assert app.state.mcp_task_store.list()[0].result is not None
        finally:
            release.set()
            finish.set()
            await task_supervisor(app).shutdown()
            await executor.aclose(force=True)
            set_task_record_store(None)
            context.reset(session_token)
            context.reset(tool_session_token)
            context.reset(token)

    asyncio.run(exercise())
