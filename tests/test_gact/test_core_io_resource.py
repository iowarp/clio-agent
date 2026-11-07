"""Core work cannot consume the native transport's shared executor."""

from __future__ import annotations

import asyncio
import contextvars
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from clio_agent.gact.core_io import CoreIO
from clio_agent.gact.off_loop import join_off_loop, run_off_loop, schedule_off_loop


@pytest.mark.asyncio
async def test_pending_core_io_leaves_default_executor_available_and_retires() -> None:
    """Retain a pending writer while finite transport I/O still runs on its own slot."""
    loop = asyncio.get_running_loop()
    previous = loop._default_executor
    default = ThreadPoolExecutor(max_workers=1, thread_name_prefix="native-transport")
    loop.set_default_executor(default)
    io = CoreIO()
    entered, release = asyncio.Event(), threading.Event()
    marker = contextvars.ContextVar("core-io-marker", default="unset")
    token = marker.set("owner-context")

    def pending() -> tuple[str, str]:
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(10), "Core writer was not released"
        return marker.get(), threading.current_thread().name

    writing = asyncio.create_task(io.run(pending))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        transport = await asyncio.wait_for(asyncio.to_thread(threading.current_thread), 2)
        assert transport.name.startswith("native-transport") and not writing.done()
        release.set()
        context, name = await writing
        assert context == "owner-context" and name.startswith("clio-core-io")
        await io.close()
        assert io._threads and not any(thread.is_alive() for thread in io._threads)
        with pytest.raises(RuntimeError, match="closed"):
            await io.run(lambda: None)
    finally:
        release.set()
        await asyncio.gather(writing, return_exceptions=True)
        await io.close()
        marker.reset(token)
        loop._default_executor = previous
        default.shutdown(wait=True)


@pytest.mark.asyncio
async def test_store_helpers_use_sdk_resource_and_preserve_context() -> None:
    """None of the standard store helpers needs a free asyncio transport slot."""
    loop = asyncio.get_running_loop()
    previous = loop._default_executor
    default = ThreadPoolExecutor(max_workers=1, thread_name_prefix="held-transport")
    loop.set_default_executor(default)
    entered, release = asyncio.Event(), threading.Event()
    marker = contextvars.ContextVar("store-helper-marker", default="unset")
    token = marker.set("owner-context")

    def occupy() -> None:
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(10), "Default executor was not released"

    def read(*, suffix: str) -> str:
        return marker.get() + suffix

    held = asyncio.create_task(asyncio.to_thread(occupy))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        assert await asyncio.wait_for(run_off_loop(read, suffix=":read"), 2) == "owner-context:read"
        assert (
            await asyncio.wait_for(join_off_loop(read, suffix=":joined"), 2)
            == "owner-context:joined"
        )
        scheduled = schedule_off_loop(lambda: read(suffix=":scheduled"), label="test-store-io")
        assert scheduled is not None
        assert await asyncio.wait_for(scheduled, 2) == "owner-context:scheduled"
        assert not release.is_set() and not held.done()
    finally:
        release.set()
        await held
        marker.reset(token)
        loop._default_executor = previous
        default.shutdown(wait=True)


@pytest.mark.asyncio
async def test_batched_items_coalesce_without_passing_a_later_operation() -> None:
    """Items queued behind a busy writer run in one call, in order, and never before an
    operation submitted after them (group commit keeps the writer FIFO).

    SABOTAGE: drop ``self._tail = None`` from ``CoreIO.submit`` and item ``c`` joins the
    first batch, running before ``plain``.
    """
    io = CoreIO()
    release = threading.Event()
    ran: list[object] = []

    def runner(items: list[str]) -> None:
        ran.append(tuple(items))

    io.submit(release.wait, 10)  # the writer is busy
    try:
        io.submit_batch(runner, "a")
        io.submit_batch(runner, "b")
        plain = io.submit(ran.append, "plain")
        io.submit_batch(runner, "c")
        release.set()
        await asyncio.wrap_future(plain)
        await io.run(lambda: None)
        assert ran == [("a", "b"), "plain", ("c",)]
        # A batch that already started takes no more items.
        started, finish = threading.Event(), threading.Event()

        def slow(items: list[str]) -> None:
            ran.append(tuple(items))
            started.set()
            assert finish.wait(10)

        ran.clear()
        io.submit_batch(slow, "x")
        assert await asyncio.to_thread(started.wait, 10)
        io.submit_batch(slow, "y")
        finish.set()
        await io.run(lambda: None)
        assert ran == [("x",), ("y",)]
    finally:
        release.set()
        await io.close()
