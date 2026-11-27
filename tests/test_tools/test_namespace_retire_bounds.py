"""A live configuration change never waits forever on a server it replaces."""

from __future__ import annotations

import asyncio
import threading
import time
import types

import pytest

from clio_agent.runtime import trace
from clio_agent.tools import mcp_namespace_executor, namespace_instances, stall_watch
from clio_agent.tools.execution import SyncMCPToolExecutor


def test_stop_on_an_idle_loop_returns_without_waiting() -> None:
    idle = asyncio.new_event_loop()  # never run: a retired executor's stopped loop
    ran: list[str] = []

    async def release() -> int:
        ran.append("ran")
        return 1

    try:
        assert asyncio.run(namespace_instances._on(idle, release())) == 0
    finally:
        idle.close()
    assert ran == []


def test_sync_stop_and_reset_are_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    events: list[str] = []
    monkeypatch.setattr(mcp_namespace_executor, "NAMESPACE_RETIRE_WAIT_SECONDS", 0.2)
    monkeypatch.setattr(trace, "event", lambda _c, fmt, *a: events.append(fmt % a))

    async def forever(*_args: object, **_kwargs: object) -> int:
        await asyncio.Event().wait()
        return 0

    monkeypatch.setattr(namespace_instances, "stop_instance", forever)
    inner = types.SimpleNamespace(_namespace_servers={"probe": object()}, reset_namespace=forever)
    executor = SyncMCPToolExecutor.__new__(SyncMCPToolExecutor)
    executor.__dict__.update(_closed=False, _loop=loop, _async_executor=inner)
    try:
        started = time.monotonic()
        assert executor.stop_namespace_instance("probe", "namespace_rebound") == 0
        executor.reset_namespace("probe")
        assert time.monotonic() - started < 5
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(5)
    timed_out = [e for e in events if e.startswith("mcp_namespace_retire_timed_out")]
    assert [e.split(" step=")[1].split()[0] for e in timed_out] == ["stop", "reset"]


def test_stall_watch_names_the_stuck_stage(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[str] = []
    monkeypatch.setattr(trace, "event", lambda _c, fmt, *a: events.append(fmt % a))
    with stall_watch.watch("tool_call_before_started", "probe_open", seconds=0.05):
        time.sleep(0.3)
    with stall_watch.watch("tool_call_before_started", "probe_fast", seconds=0.05):
        stall_watch.passed()
        time.sleep(0.2)
    assert len(events) == 1
    assert "stage=tool_call_before_started name=probe_open" in events[0]
    assert "test_stall_watch_names_the_stuck_stage" in events[0]
