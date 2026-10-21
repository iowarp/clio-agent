"""Workspace lease contention must not prevent MCP work on the application loop."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from clio_agent.agent import ClioAgent
from clio_agent.gact import context
from clio_agent.gact.runtime.globals import _async_tool_session_context, _gact_app_context
from clio_agent.plugins.servers import register_server_set, server_set_declarations
from clio_agent.tools.execution import get_active_tool_server_set, get_active_tool_workspace_root
from clio_agent.tools.workspace_root import canonical_workspace_root


@pytest.fixture
def owner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Any, ClioAgent, str, str]:
    """Use the production lease owner without starting unrelated model clients."""
    agent = object.__new__(ClioAgent)
    root = str(tmp_path)
    app = SimpleNamespace(
        state=SimpleNamespace(
            agent=agent,
            sessions={"session": SimpleNamespace(workspace_id="workspace")},
            workspaces={"workspace": SimpleNamespace(root_path=root)},
        )
    )
    server_set = register_server_set(
        {
            "lease": {
                "spec": {"type": "stdio", "command": "lease-server", "args": []},
                "root": str(tmp_path / "plugin"),
                "plugin": "lease-test",
            }
        }
    )

    def session_server_set(_app: Any, sid: str) -> str:
        assert sid == "session"
        return server_set

    monkeypatch.setattr("clio_agent.gact.agents.resolution.session_server_set", session_server_set)
    return app, agent, root, server_set


@pytest.mark.asyncio
async def test_warmup_lock_can_wait_for_application_loop_while_turn_acquires_lease(
    owner: tuple[Any, ClioAgent, str, str],
) -> None:
    app, agent, root, server_set = owner
    loop = asyncio.get_running_loop()
    lock, _, leases = agent._workspace_state()
    holding = threading.Event()
    attempted = asyncio.Event()
    callback_ran = asyncio.Event()

    async def mcp_callback() -> None:
        await attempted.wait()
        callback_ran.set()

    def warmup() -> None:
        with lock:
            holding.set()
            # Production warmup waits for MCP work on this same loop. A blocked
            # loop makes this future fail, rather than hanging the test process.
            asyncio.run_coroutine_threadsafe(mcp_callback(), loop).result(timeout=2)

    warm = asyncio.create_task(asyncio.to_thread(warmup))
    assert await asyncio.to_thread(holding.wait, 2)
    try:
        with _gact_app_context(app):
            attempted.set()
            async with _async_tool_session_context("session"):
                assert callback_ran.is_set()
                assert leases == {canonical_workspace_root(root): 1}
                assert context.active_tool_session_id() == "session"
                assert get_active_tool_workspace_root() == root
                assert get_active_tool_server_set() == server_set
                assert set(server_set_declarations(server_set)) == {"lease-test"}
            assert context.active_tool_session_id() == ""
            assert get_active_tool_server_set() == ""
        await warm
        assert leases == {}
    finally:
        await warm


@pytest.mark.asyncio
async def test_repeated_cancellation_during_acquisition_releases_the_actual_lease(
    owner: tuple[Any, ClioAgent, str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    app, agent, _root, _server_set = owner
    lock, _, leases = agent._workspace_state()
    started = threading.Event()
    entered_body = False
    original = agent.lease_workspace_fleet

    @contextmanager
    def acquiring(root: str) -> Iterator[None]:
        started.set()
        with original(root):
            yield

    monkeypatch.setattr(agent, "lease_workspace_fleet", acquiring)

    async def turn() -> None:
        nonlocal entered_body
        with _gact_app_context(app):
            async with _async_tool_session_context("session"):
                entered_body = True

    lock.acquire()
    task = asyncio.create_task(turn())
    try:
        assert await asyncio.to_thread(started.wait, 2)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
    finally:
        lock.release()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not entered_body and leases == {}


@pytest.mark.asyncio
async def test_cancellation_during_release_finishes_cleanup_before_settling(
    owner: tuple[Any, ClioAgent, str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    app, agent, _root, _server_set = owner
    lock, _, leases = agent._workspace_state()
    releasing = threading.Event()
    original = agent.lease_workspace_fleet

    @contextmanager
    def tracked(root: str) -> Iterator[None]:
        with original(root):
            try:
                yield
            finally:
                releasing.set()

    monkeypatch.setattr(agent, "lease_workspace_fleet", tracked)

    async def turn() -> None:
        with _gact_app_context(app):
            async with _async_tool_session_context("session"):
                assert lock.acquire(blocking=False)

    task = asyncio.create_task(turn())
    try:
        assert await asyncio.to_thread(releasing.wait, 2)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done() and leases
    finally:
        lock.release()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert leases == {}


@pytest.mark.asyncio
async def test_lease_rejection_does_not_enter_or_release_an_unacquired_lease(
    owner: tuple[Any, ClioAgent, str, str],
) -> None:
    app, agent, _root, _server_set = owner
    agent._source_policy_changing = True
    with _gact_app_context(app), pytest.raises(RuntimeError, match="connected-data access"):
        async with _async_tool_session_context("session"):
            pytest.fail("a rejected lease must not enter the turn")
    assert agent._workspace_state()[2] == {}
