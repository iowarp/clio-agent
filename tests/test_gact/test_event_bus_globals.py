"""Global (session_id="") events must reach per-session SSE subscribers.

clio publishes lm.provider.* and mcp.server.* with session_id="". The bus
used to fan events only to same-session subscribers, so those never reached
an open session stream (provider-change toasts never fired). subscribe()
now fans the session queue into the global bucket too.
"""

from __future__ import annotations

import asyncio

from clio_agent.gact.events import Event, EventBus


async def test_session_subscriber_receives_global_event() -> None:
    bus = EventBus()
    received: list[Event] = []

    async def consume() -> None:
        async for ev in bus.subscribe("sess_1"):
            received.append(ev)
            if len(received) >= 2:
                break

    task = asyncio.create_task(consume())
    await asyncio.sleep(0)  # let subscribe() register the queue
    bus.publish(Event(type="session.updated", session_id="sess_1", payload={}))
    bus.publish(Event(type="lm.provider.changed", session_id="", payload={"provider_id": "alcf"}))
    await asyncio.wait_for(task, timeout=1.0)

    types = [e.type for e in received]
    assert "session.updated" in types
    assert "lm.provider.changed" in types  # the global reached the session stream


async def test_global_event_replayed_to_late_subscriber() -> None:
    bus = EventBus()
    # Global event published BEFORE anyone subscribes — must still replay.
    bus.publish(Event(type="lm.provider.failed", session_id="", payload={}))
    received: list[Event] = []

    async def consume() -> None:
        async for ev in bus.subscribe("sess_2"):
            received.append(ev)
            break

    task = asyncio.create_task(consume())
    await asyncio.wait_for(task, timeout=1.0)
    assert [e.type for e in received] == ["lm.provider.failed"]


async def test_other_sessions_stay_isolated() -> None:
    bus = EventBus()
    received: list[Event] = []

    async def consume() -> None:
        async for ev in bus.subscribe("sess_a"):
            received.append(ev)
            break

    task = asyncio.create_task(consume())
    await asyncio.sleep(0)
    # A different session's event must NOT leak in; the global one must.
    bus.publish(Event(type="session.updated", session_id="sess_b", payload={}))
    bus.publish(Event(type="lm.provider.changed", session_id="", payload={}))
    await asyncio.wait_for(task, timeout=1.0)
    assert received[0].type == "lm.provider.changed"
