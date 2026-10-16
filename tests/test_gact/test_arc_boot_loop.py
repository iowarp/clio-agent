"""First model binds share ARC boot safely across real event loops."""

from __future__ import annotations

import asyncio
import threading
from typing import Any

import pytest
from fastapi import FastAPI

from clio_agent.gact import sandbox_setup, server_boot


@pytest.mark.asyncio
async def test_executor_bind_joins_pending_boot_on_the_application_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A temporary bind loop must not await the server loop's Task directly."""
    app = FastAPI()
    owner = asyncio.get_running_loop()
    app.state.mcp_app_loop = owner
    entered = threading.Event()
    release = threading.Event()
    second_waiter = asyncio.Event()
    loops: list[asyncio.AbstractEventLoop] = []
    calls = 0
    result = object()

    async def prepare(_: FastAPI) -> None:
        loops.append(asyncio.get_running_loop())
        if len(loops) == 2:
            second_waiter.set()

    def construct(_: FastAPI) -> Any:
        nonlocal calls
        calls += 1
        entered.set()
        assert release.wait(5)
        app.state.arc = result
        return result

    monkeypatch.setattr(sandbox_setup, "prepare_sandbox_before_tools", prepare)
    monkeypatch.setattr(server_boot, "process_arc", construct)
    boot = asyncio.create_task(server_boot.process_arc_off_loop(app))
    worker: asyncio.Task[Any] | None = None
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        worker = asyncio.create_task(
            asyncio.to_thread(lambda: asyncio.run(server_boot.process_arc_off_loop(app)))
        )
        await asyncio.wait_for(second_waiter.wait(), 5)
    finally:
        release.set()
        if worker is not None:
            assert await worker is result
        assert await boot is result
    assert calls == 1
    assert loops == [owner, owner]


@pytest.mark.asyncio
async def test_cancelled_executor_bind_preserves_shared_boot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancelling a bind's bridge cannot cancel the server-owned context task."""
    app = FastAPI()
    app.state.mcp_app_loop = asyncio.get_running_loop()
    release = threading.Event()
    entered = threading.Event()
    waiter_entered = asyncio.Event()
    cancel_worker = threading.Event()
    result = object()
    calls = 0

    async def prepare(_: FastAPI) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            waiter_entered.set()

    def construct(_: FastAPI) -> Any:
        entered.set()
        assert release.wait(5)
        app.state.arc = result
        return result

    async def bind_then_cancel() -> None:
        task = asyncio.create_task(server_boot.process_arc_off_loop(app))
        await asyncio.to_thread(cancel_worker.wait, 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    monkeypatch.setattr(sandbox_setup, "prepare_sandbox_before_tools", prepare)
    monkeypatch.setattr(server_boot, "process_arc", construct)
    boot = asyncio.create_task(server_boot.process_arc_off_loop(app))
    worker: asyncio.Task[None] | None = None
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        worker = asyncio.create_task(asyncio.to_thread(lambda: asyncio.run(bind_then_cancel())))
        await asyncio.wait_for(waiter_entered.wait(), 5)
        cancel_worker.set()
        await worker
        assert not app.state.arc_boot_task.cancelled()
        assert not app.state.arc_boot_task.done()
    finally:
        cancel_worker.set()
        release.set()
        if worker is not None:
            await worker
        assert await boot is result


@pytest.mark.asyncio
async def test_executor_bind_retries_a_failed_boot(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failure propagates and the next bind creates a fresh context on the owner loop."""
    app = FastAPI()
    app.state.mcp_app_loop = asyncio.get_running_loop()
    calls = 0
    result = object()

    async def prepare(_: FastAPI) -> None:
        return None

    def construct(_: FastAPI) -> Any:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("first attach failed")
        app.state.arc = result
        return result

    monkeypatch.setattr(sandbox_setup, "prepare_sandbox_before_tools", prepare)
    monkeypatch.setattr(server_boot, "process_arc", construct)
    with pytest.raises(RuntimeError, match="first attach failed"):
        await asyncio.to_thread(lambda: asyncio.run(server_boot.process_arc_off_loop(app)))
    assert (
        await asyncio.to_thread(lambda: asyncio.run(server_boot.process_arc_off_loop(app)))
        is result
    )
    assert calls == 2
