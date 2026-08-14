"""Regression tests for iowarp/clio-agent#1214 (S1, plus the opus adversarial
review's D1/D5 fix-first findings).

``EventBus._deliver`` used to swallow subscriber-queue overflow with a bare
``except asyncio.QueueFull: pass`` — no counter, no log, no stream_audit row,
no gap marker on the wire. That is a no-silent-fallback violation (the
``stream_fallback`` typed-reason convention this repo follows everywhere
else): a dropped ``message.part.added``/``message.part.delta`` reproduces
observed transcript defects (missing tool calls, truncated thinking) and,
without a counter, is invisible after the fact.

This pins the fix: a per-session drop counter (:meth:`EventBus.dropped_total`),
a decade-boundary-throttled ``logger.warning`` (D1 below), one
``bus.queue_full`` stream_audit row per drop, and one recovery
``logger.info`` line when a drop streak ends. Sabotage twin: reverting
``_deliver`` to the bare ``except asyncio.QueueFull: pass`` makes every
assertion below go red — there is no counter to read and no audit row to
find.

D1 (review finding): the FIRST fix shipped here reset an "announced" burst
flag on every clean delivery. Two review probes broke it: a slow-but-
draining consumer (drop, catch up, drop, catch up, ...) re-armed the warning
almost every cycle (39 warnings for 77 drops), and two subscribers on one
session — one stuck, one healthy — reset the flag via the healthy queue's
success even while the stuck queue kept failing (18 warnings for 18 drops).
Replaced with decade-boundary escalation on the cumulative ``dropped_total``
(warn at 1, 10, 100, 1000, ...), which is immune to interleaving noise
because it only depends on a monotonic count, plus ONE ``logger.info`` line
when a session's drop streak actually ends (evaluated once per
``_deliver()`` call across ALL of a session's subscriber queues, not per
queue, so a healthy sibling queue's success can no longer mask an ongoing
drop on a stuck queue).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

import pytest

from clio_agent.gact.events import Event, EventBus, _is_decade_boundary


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


def _warning_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.getMessage().startswith("eventbus_queue_full ")]


def _recovery_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.getMessage().startswith("eventbus_queue_full_recovered")]


# ---------------------------------------------------------------------------
# Decade-boundary helper (pure).
# ---------------------------------------------------------------------------


def test_is_decade_boundary() -> None:
    assert [n for n in range(0, 25) if _is_decade_boundary(n)] == [1, 10]
    assert _is_decade_boundary(1) is True
    assert _is_decade_boundary(10) is True
    assert _is_decade_boundary(100) is True
    assert _is_decade_boundary(1000) is True
    for not_boundary in (0, -1, 2, 9, 11, 50, 99, 101, 999, 1001):
        assert _is_decade_boundary(not_boundary) is False


# ---------------------------------------------------------------------------
# Counter + audit row (unchanged contract).
# ---------------------------------------------------------------------------


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
        # is empty -> red. Every drop gets its OWN row regardless of decade
        # boundary -- the audit trail is exact even though the log is throttled.
        assert len(drop_rows) == 3
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


# ---------------------------------------------------------------------------
# D1 -- decade-boundary warning escalation (replaces the degenerate burst flag).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_queue_full_warns_at_decade_boundaries_not_per_event(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A stalled consumer that never drains produces warnings at
    dropped_total == 1, 10, 100 (decade boundaries), never one per event."""

    bus = EventBus(queue_capacity=1)
    task = await _attach_stalled_subscriber(bus, "s3")
    try:
        with caplog.at_level(logging.INFO, logger="clio_agent.gact.events"):
            # publish #1 fills the capacity-1 queue; #2-121 (120 events) overflow
            # it, crossing the 1, 10, and 100 decade boundaries.
            for i in range(121):
                bus.publish(Event(type="message.part.delta", session_id="s3", payload={"i": i}))
        assert bus.dropped_total("s3") == 120
        warnings = _warning_records(caplog)
        # Sabotage: log a warning on every QueueFull -> len(warnings) == 120 -> red.
        # Sabotage: revert to the degenerate "reset every clean delivery" burst
        # flag -> irrelevant here (single subscriber never delivers cleanly once
        # stalled) but the decade-count assertion still pins the new mechanism.
        assert [int(r.getMessage().rsplit("dropped_total=", 1)[1]) for r in warnings] == [
            1,
            10,
            100,
        ]
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_slow_but_draining_oscillation_does_not_spam_warnings(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Review probe D1: a consumer that drains BETWEEN overflows (not stuck,
    just slow) used to re-arm the old "reset on every clean delivery" burst
    flag almost every cycle (39 warnings for 77 drops in review). Reproduced
    here as 77 fill/overflow/drain cycles -- decade escalation caps this at
    2 warnings (at dropped_total 1 and 10), independent of how the drops and
    successful deliveries interleave.
    """

    bus = EventBus(queue_capacity=1)
    q: asyncio.Queue[Event] = asyncio.Queue(maxsize=1)
    # White-box queue attach: deterministic control over draining, matching
    # the pattern the existing burst/recovery test below already uses.
    bus._subs["s_oscillate"].append(q)

    with caplog.at_level(logging.INFO, logger="clio_agent.gact.events"):
        for i in range(77):
            bus.publish(  # fills the empty queue -- delivered cleanly
                Event(type="a", session_id="s_oscillate", payload={"i": i, "half": "fill"})
            )
            bus.publish(  # queue already full from the line above -- drops
                Event(type="a", session_id="s_oscillate", payload={"i": i, "half": "drop"})
            )
            await q.get()  # the "slow but draining" consumer catches up

    assert bus.dropped_total("s_oscillate") == 77
    warnings = _warning_records(caplog)
    # Sabotage: keep the old per-clean-delivery burst reset -> every cycle's
    # drop is preceded by a reset (that cycle's successful fill) -> 77
    # warnings, one per drop -> this assertion goes red.
    assert len(warnings) == 2
    recoveries = _recovery_records(caplog)
    # Each cycle's "fill" IS a full clean _deliver() cycle (the queue was
    # empty from the prior drain), so it detects and logs the PRECEDING
    # cycle's drop streak as recovered -- 76 of the 77 streaks (cycle 0's
    # fill has nothing to recover from; the 77th/last cycle's drop is never
    # followed by another publish within this test, so its streak has no
    # chance to be observed as recovered). This is the expected, bounded
    # cost of a genuinely oscillating consumer; it is the WARNING volume
    # (the thing an on-call human reads) that had to stop scaling with drop
    # count, not the info-level recovery bookkeeping.
    assert len(recoveries) == 76


@pytest.mark.asyncio
async def test_two_subscribers_one_stuck_one_healthy_does_not_spam_warnings(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Review probe D1: two subscribers on ONE session -- one stuck (never
    drains), one healthy (always drains before the next publish). The old
    design reset the burst flag on the healthy queue's per-queue success
    even while the stuck queue kept failing in the SAME _deliver() call,
    producing 18 warnings for 18 drops. Recovery/warning gating is now
    evaluated ONCE per _deliver() call, across every subscriber queue for
    the session, so the still-dropping stuck queue keeps the streak "active"
    regardless of the healthy sibling's success.
    """

    bus = EventBus(queue_capacity=1)
    stuck: asyncio.Queue[Event] = asyncio.Queue(maxsize=1)
    healthy: asyncio.Queue[Event] = asyncio.Queue(maxsize=1)
    bus._subs["s_dual"].append(stuck)
    bus._subs["s_dual"].append(healthy)
    # Prime the stuck queue full ONCE so every publish below overflows it;
    # the healthy queue starts empty and is drained after every publish.
    stuck.put_nowait(Event(type="seed", session_id="s_dual", payload={}))

    with caplog.at_level(logging.INFO, logger="clio_agent.gact.events"):
        for i in range(18):
            bus.publish(Event(type="a", session_id="s_dual", payload={"i": i}))
            await healthy.get()  # the healthy subscriber always keeps up

    # The stuck subscriber dropped all 18; the healthy one dropped none, but
    # the SESSION-level counter (and the gating below) is what matters.
    assert bus.dropped_total("s_dual") == 18
    warnings = _warning_records(caplog)
    # Sabotage: reset the burst/decade gate per-queue instead of per-delivery
    # -> the healthy queue's per-publish success resets it every cycle ->
    # 18 warnings (one per drop) -> this assertion goes red.
    assert len(warnings) == 2  # decade boundaries 1 and 10 within [1, 18]
    # The stuck queue never stops dropping, so the streak never "recovers".
    assert _recovery_records(caplog) == []


# ---------------------------------------------------------------------------
# Recovery info line + burst-flag bookkeeping.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_drop_streak_recovery_logs_one_info_line_and_rearms(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A drop streak that ends (queue drains, a later publish delivers
    cleanly) logs exactly ONE recovery info line and re-arms the streak
    flag so a LATER overflow is tracked as a fresh streak."""

    bus = EventBus(queue_capacity=1)
    q: asyncio.Queue[Event] = asyncio.Queue(maxsize=1)
    bus._subs["s4"].append(q)

    with caplog.at_level(logging.INFO, logger="clio_agent.gact.events"):
        bus.publish(Event(type="a", session_id="s4", payload={}))  # fills queue
        bus.publish(Event(type="a", session_id="s4", payload={}))  # overflow #1 -> streak starts
        assert bus.dropped_total("s4") == 1
        assert bus._drop_burst_active["s4"] is True
        assert _recovery_records(caplog) == []

        await q.get()  # drain (a "clean delivery" from the consumer's perspective)
        bus.publish(Event(type="a", session_id="s4", payload={}))  # delivered cleanly -> recovers
        assert bus._drop_burst_active["s4"] is False
        assert bus.dropped_total("s4") == 1  # unchanged -- this publish did not drop
        # Sabotage: drop the _note_delivery_recovered call (or never gate it on
        # _drop_burst_active) -> this list is empty, or grows on every clean
        # delivery instead of once -> either way this assertion goes red.
        assert len(_recovery_records(caplog)) == 1

        await q.get()  # drain again -- must NOT log a second recovery line
        bus.publish(Event(type="a", session_id="s4", payload={}))
        assert len(_recovery_records(caplog)) == 1

        bus.publish(Event(type="a", session_id="s4", payload={}))  # overflow #2 -> new streak
        assert bus.dropped_total("s4") == 2
        assert bus._drop_burst_active["s4"] is True
