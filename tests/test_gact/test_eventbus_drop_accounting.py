"""Regression tests for iowarp/clio-agent#1214.

``EventBus._deliver`` used to swallow subscriber-queue overflow with a bare
``except asyncio.QueueFull: pass`` — no counter, no log, no stream_audit row,
no gap marker on the wire. That is a no-silent-fallback violation (the
``stream_fallback`` typed-reason convention this repo follows everywhere
else): a dropped ``message.part.added``/``message.part.delta`` reproduces
observed transcript defects (missing tool calls, truncated thinking) and,
without a counter, is invisible after the fact.

This pins the fix: a per-session drop counter (:meth:`EventBus.dropped_total`),
one ``logger.warning`` per burst (not per event), and one ``bus.queue_full``
stream_audit row per drop. Sabotage twin: reverting ``_deliver`` to the bare
``except asyncio.QueueFull: pass`` makes every assertion below go red — there
is no counter to read and no audit row to find.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

import pytest

from clio_agent.gact.events import Event, EventBus


async def _attach_stalled_subscriber(bus: EventBus, session_id: str) -> asyncio.Task[None]:
    """Register a live subscriber queue for ``session_id`` that never drains it.

    Drives the REAL ``subscribe()`` async generator (not private state): the
    task runs until it registers its queue and starts awaiting the first
    item, then never resumes — a faithful stand-in for a slow/stuck SSE
    consumer. Caller is responsible for cancelling the returned task.
    """

    async def stalled_reader() -> None:
        async for _ev in bus.subscribe(session_id):
            await asyncio.Event().wait()  # suspends forever; never drains again

    task = asyncio.create_task(stalled_reader())
    # Yield once so subscribe() runs up to its `await q.get()` and the queue
    # is registered in bus._subs before any publish below.
    await asyncio.sleep(0)
    return task


@pytest.mark.asyncio
async def test_queue_full_drop_counter_and_audit_row(monkeypatch: Any) -> None:
    """Failing-first per #1214: capacity=2, 5 publishes, no draining consumer
    -> dropped_total == 3 and an audit row was emitted."""

    audits: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        "clio_agent.gact.events.stream_audit",
        lambda stage, **fields: audits.append((stage, fields)),
    )

    bus = EventBus(queue_capacity=2)
    task = await _attach_stalled_subscriber(bus, "s1")
    try:
        for i in range(5):
            bus.publish(Event(type="message.part.delta", session_id="s1", payload={"i": i}))

        # First 2 publishes fill the capacity-2 queue; the next 3 overflow it
        # (the reader never resumed to drain — no `await` happened between
        # publishes above).
        assert bus.dropped_total("s1") == 3

        drop_rows = [fields for stage, fields in audits if stage == "bus.queue_full"]
        # Sabotage: remove the stream_audit call in _record_drop -> this list
        # is empty -> red.
        assert drop_rows, "expected at least one bus.queue_full stream_audit row"
        assert drop_rows[-1]["session_id"] == "s1"
        assert drop_rows[-1]["event_type"] == "message.part.delta"
        assert drop_rows[-1]["dropped_total"] == 3
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_no_drops_when_consumer_keeps_up() -> None:
    """A session that never overflows its queue reports dropped_total == 0."""

    bus = EventBus(queue_capacity=8)
    received: list[Event] = []

    async def reader() -> None:
        async for ev in bus.subscribe("s2"):
            received.append(ev)
            if len(received) >= 3:
                break

    task = asyncio.create_task(reader())
    await asyncio.sleep(0)
    for i in range(3):
        bus.publish(Event(type="x", session_id="s2", payload={"i": i}))
    await asyncio.wait_for(task, timeout=2.0)

    assert bus.dropped_total("s2") == 0
    assert len(received) == 3


@pytest.mark.asyncio
async def test_queue_full_logs_one_warning_per_burst_not_per_event(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Burst semantics: a stalled consumer that never drains produces ONE
    logger.warning for the whole overflow stretch, not one per dropped event
    -- even though every drop still gets its own stream_audit row (pinned by
    the counter/audit test above)."""

    bus = EventBus(queue_capacity=1)
    task = await _attach_stalled_subscriber(bus, "s3")
    try:
        with caplog.at_level(logging.WARNING, logger="clio_agent.gact.events"):
            for i in range(6):
                bus.publish(Event(type="message.part.delta", session_id="s3", payload={"i": i}))
        # capacity=1: publish #1 fills it, publishes #2-6 (5 events) overflow.
        assert bus.dropped_total("s3") == 5
        warnings = [
            r for r in caplog.records if r.getMessage().startswith("eventbus_queue_full")
        ]
        # Sabotage: log a warning on every QueueFull instead of once-per-burst
        # -> len(warnings) == 5 -> this assertion red.
        assert len(warnings) == 1
        assert "session_id=s3" in warnings[0].getMessage()
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_drop_burst_resets_after_a_clean_delivery() -> None:
    """A drop burst that ends (queue drains, a later publish delivers cleanly)
    re-arms the warning: the NEXT overflow stretch logs again."""

    bus = EventBus(queue_capacity=1)
    q: asyncio.Queue[Event] = asyncio.Queue(maxsize=1)
    # White-box: attach a queue we control the draining of directly, so we can
    # deterministically interleave "drain" with "overflow again" without
    # racing a real consumer task's scheduling.
    bus._subs["s4"].append(q)

    bus.publish(Event(type="a", session_id="s4", payload={}))  # fills queue
    bus.publish(Event(type="a", session_id="s4", payload={}))  # overflow #1 -> burst starts
    assert bus.dropped_total("s4") == 1
    assert bus._drop_burst_active["s4"] is True

    # Drain the queue (a "clean delivery" from the consumer's perspective).
    await q.get()
    bus.publish(Event(type="a", session_id="s4", payload={}))  # delivered cleanly
    assert bus._drop_burst_active["s4"] is False
    assert bus.dropped_total("s4") == 1  # unchanged — this publish did not drop

    bus.publish(Event(type="a", session_id="s4", payload={}))  # overflow #2 -> new burst
    assert bus.dropped_total("s4") == 2
    assert bus._drop_burst_active["s4"] is True
