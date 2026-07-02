"""Regression tests for iowarp/clio-agent#758.

``EventBus.publish`` is called from worker threads (the MCP
tool-observer thread, the LM-bind thread, executor threads emitting
semantic events) while the subscriber queues are ``asyncio.Queue``
instances owned by the server's event loop. ``asyncio.Queue`` is not
thread-safe: a cross-thread ``put_nowait`` mutates loop-owned state
and does not reliably wake a waiting getter. The bus must bridge
foreign-thread publishes onto the owning loop via
``loop.call_soon_threadsafe``.
"""

from __future__ import annotations

import asyncio
import threading
from unittest.mock import patch

import pytest

from clio_agent.gact.events import Event, EventBus


@pytest.mark.asyncio
async def test_publish_from_foreign_thread_routes_via_call_soon_threadsafe() -> None:
    """A foreign-thread publish must be bridged with call_soon_threadsafe.

    The raw wakeup hazard (a loop that never notices a cross-thread
    ``put_nowait``) is timing-dependent, so the deterministic assertion
    is on the mechanism: publishing from a worker thread must go through
    the owning loop's threadsafe bridge, and the event must still reach
    an async subscriber.
    """

    bus = EventBus()
    loop = asyncio.get_running_loop()
    received: list[Event] = []

    async def reader() -> None:
        async for ev in bus.subscribe("s1"):
            received.append(ev)
            break

    task = asyncio.create_task(reader())
    # Yield so the subscriber registers (and binds the owning loop).
    await asyncio.sleep(0)

    threadsafe_callers: list[str] = []
    original = loop.call_soon_threadsafe

    def spying_call_soon_threadsafe(callback, *args, **kwargs):  # type: ignore[no-untyped-def]
        threadsafe_callers.append(threading.current_thread().name)
        return original(callback, *args, **kwargs)

    with patch.object(loop, "call_soon_threadsafe", new=spying_call_soon_threadsafe):
        worker = threading.Thread(
            target=lambda: bus.publish(
                Event(type="tool.part.updated", session_id="s1", payload={})
            ),
            name="eventbus-test-worker",
        )
        worker.start()
        worker.join(timeout=5.0)
    assert not worker.is_alive()

    await asyncio.wait_for(task, timeout=2.0)
    assert [ev.type for ev in received] == ["tool.part.updated"]
    # The loop-owned queue/history mutation MUST have been bridged from
    # the worker thread via call_soon_threadsafe — not done cross-thread.
    assert "eventbus-test-worker" in threadsafe_callers


@pytest.mark.asyncio
async def test_threaded_publish_stress_delivers_every_event() -> None:
    """Multi-threaded publishers: no missed wakeups, no lost events."""

    per_thread = 50
    thread_count = 4
    total = per_thread * thread_count
    bus = EventBus(queue_capacity=total * 2, history_per_session=total * 2)
    received: list[Event] = []

    async def reader() -> None:
        async for ev in bus.subscribe("s1"):
            received.append(ev)
            if len(received) >= total:
                break

    task = asyncio.create_task(reader())
    await asyncio.sleep(0)

    def worker() -> None:
        for _ in range(per_thread):
            bus.publish(Event(type="x", session_id="s1", payload={}))

    threads = [
        threading.Thread(target=worker, name=f"eventbus-stress-{i}") for i in range(thread_count)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10.0)

    await asyncio.wait_for(task, timeout=10.0)
    assert len(received) == total


def test_publish_without_bound_loop_still_records_history() -> None:
    """Before any subscriber binds a loop, publish records replay history.

    This is the normal startup path (POST /messages can land before the
    first SSE subscriber attaches); it must keep working synchronously.
    """

    bus = EventBus()
    bus.publish(Event(type="message.created", session_id="s1", payload={}))
    assert [ev.type for ev in bus._history["s1"]] == ["message.created"]
