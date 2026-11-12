"""Closing an entry while its abandoned stream is still disconnecting must not strand it.

Live (develop, 4 concurrent conversations, 2026-11-12): an abandoned stream schedules
``_mark_dead_and_reset`` on the entry's owner loop; a queued connect then reclaims the
dead entry and ``close_nonblocking`` schedules a second reset, which found the client
already dropped, returned at once, and its done-callback stopped the loop under the
first reset's ``disconnect()``. Four such resets held all four connect slots with no
client alive (zombie CLI children), so every later model call waited for a slot until
it was cancelled: one turn waited 14 minutes.
"""

from __future__ import annotations

import asyncio
import threading
import time

from clio_agent.providers import claude_code_sessions as ccs


class SlowDisconnectClient:
    """A client whose disconnect takes a moment, like a CLI child shutting down."""

    def __init__(self) -> None:
        self.disconnected = threading.Event()

    async def disconnect(self) -> None:
        await asyncio.sleep(0.2)
        self.disconnected.set()


def test_close_during_an_abandoned_disconnect_releases_the_slot() -> None:
    slots = threading.Semaphore(1)
    assert slots.acquire(blocking=False)  # the connect this entry made
    entry = ccs._StreamClientEntry(connect_slots=slots)
    entry._ensure_loop()
    client = SlowDisconnectClient()
    entry._client = client

    loop = entry._loop
    asyncio.run_coroutine_threadsafe(entry._mark_dead_and_reset(), loop)  # abandon path
    time.sleep(0.05)  # the first reset is now inside disconnect()
    entry.close_nonblocking()  # the reclaim path: reset again, then stop the loop

    assert client.disconnected.wait(2), "the in-flight disconnect was stranded"
    deadline = time.monotonic() + 2
    while not slots.acquire(blocking=False):
        assert time.monotonic() < deadline, "the connect slot was never released"
        time.sleep(0.01)
    deadline = time.monotonic() + 2
    while loop.is_running():
        assert time.monotonic() < deadline, "the owner loop was never stopped"
        time.sleep(0.01)


def test_a_second_reset_with_nothing_in_flight_returns_at_once() -> None:
    slots = threading.Semaphore(1)
    entry = ccs._StreamClientEntry(connect_slots=slots)
    asyncio.run(entry._areset_client())  # no client, nothing in flight
    assert slots.acquire(blocking=False)  # nothing was released twice or taken
    assert not slots.acquire(blocking=False)
