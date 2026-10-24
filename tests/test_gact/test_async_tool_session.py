"""Fleet lock contention must leave the server loop free to finish warmup."""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from clio_agent.gact import context
from clio_agent.gact.runtime.tool_session import (
    _async_tool_session_context as async_tool_session_context,
)
from clio_agent.tools.execution import get_active_tool_workspace_root
from clio_agent.tools.workspace_root import canonical_workspace_root
from tests.test_gact.test_workspace_tool_executor import _bare_agent


@pytest.fixture(autouse=True)
def _no_plugin_servers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fleet leasing is the subject: the session's agent mounts no plugin servers."""

    monkeypatch.setattr("clio_agent.gact.agents.resolution.session_server_set", lambda *_a: "")


def _owner(root: Path) -> tuple[Any, Any]:
    agent = _bare_agent()
    owner = SimpleNamespace(
        state=SimpleNamespace(
            agent=agent,
            sessions=SimpleNamespace(get=lambda sid: SimpleNamespace(workspace_id="owned")),
            workspaces=SimpleNamespace(get=lambda wid: SimpleNamespace(root_path=str(root))),
        )
    )
    return owner, agent


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["entry", "exit"])
async def test_contended_fleet_lease_does_not_block_application_loop(
    tmp_path: Path, phase: str
) -> None:
    owner, agent = _owner(tmp_path)
    lock, _, leases = agent._workspace_state()
    loop = asyncio.get_running_loop()
    held = asyncio.Event()
    release = threading.Event()
    holder_completed = False

    def warmup() -> None:
        nonlocal holder_completed
        with lock:
            loop.call_soon_threadsafe(held.set)
            holder_completed = release.wait(3)

    async def turn() -> None:
        nonlocal holder
        token = context.set_app(owner)
        try:
            if phase == "entry":
                holder = asyncio.create_task(asyncio.to_thread(warmup))
                await held.wait()
            async with async_tool_session_context("owned-session"):
                assert leases == {canonical_workspace_root(tmp_path): 1}
                assert context.active_tool_session_id() == "owned-session"
                assert get_active_tool_workspace_root() == str(tmp_path)
                if phase == "exit":
                    holder = asyncio.create_task(asyncio.to_thread(warmup))
                    await held.wait()
                # The callback can only release warmup if admission/release
                # yields the application loop instead of blocking on its lock.
                loop.call_soon(release.set)
        finally:
            context.reset(token)

    holder: asyncio.Task[None] | None = None
    try:
        if phase == "entry":
            # Schedule release after the turn has yielded on its contended entry.
            async def release_entry() -> None:
                await held.wait()
                loop.call_soon(release.set)

            releaser = asyncio.create_task(release_entry())
        await asyncio.wait_for(turn(), timeout=5)
        if phase == "entry":
            await releaser
    finally:
        release.set()
        if holder is not None:
            await holder
    assert holder_completed
    assert leases == {}
    assert context.active_tool_session_id() != "owned-session"


@pytest.mark.asyncio
async def test_cancellation_during_fleet_entry_releases_the_late_lease(
    tmp_path: Path,
) -> None:
    owner, agent = _owner(tmp_path)
    lock, _, leases = agent._workspace_state()
    loop = asyncio.get_running_loop()
    attempted = asyncio.Event()
    original_lease = agent.lease_workspace_fleet

    def lease(root: str) -> Any:
        loop.call_soon_threadsafe(attempted.set)
        return original_lease(root)

    agent.lease_workspace_fleet = lease
    body_entered = False

    async def turn() -> None:
        nonlocal body_entered
        token = context.set_app(owner)
        try:
            async with async_tool_session_context("cancelled-session"):
                body_entered = True
        finally:
            context.reset(token)

    with lock:
        task = asyncio.create_task(turn())
        await attempted.wait()
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=5)
    assert body_entered is False
    assert leases == {}
