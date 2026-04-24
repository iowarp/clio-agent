"""GACT v0.2 event bus for SSE streams.

Tiny per-session pub/sub. Producers (POST /messages, future tool
runs) publish events; SSE subscribers consume them in arrival
order. The bus is in-process — fine for a single-tenant
clio-agent-gact server (the only deployment shape today). A future
multi-process / multi-replica setup would replace this with
Redis pub/sub or NATS, but the publish/subscribe API stays the
same so the routes don't need to change.

Event envelope mirrors SPEC §7.2:

    {
      "type": "<event_type>",
      "occurred_at": "<ISO-8601 UTC>",
      "payload": { ... }
    }

The SSE wire format (per SPEC §7.2 + RFC 7240) is::

    event: <event_type>
    id: <monotonic event id>
    data: <json envelope>

Subscribers see the JSON envelope directly; the SSE response layer
adds the event:/id:/data: lines.
"""

from __future__ import annotations

import asyncio
import itertools
import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, AsyncIterator


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


_event_id_counter = itertools.count(1)


def _next_event_id() -> int:
    """Monotonic id used as the SSE ``id:`` line so clients can
    resume via ``Last-Event-ID`` after a reconnect (SPEC §7.1)."""

    return next(_event_id_counter)


class Event:
    """In-memory event record."""

    __slots__ = ("id", "type", "session_id", "occurred_at", "payload")

    def __init__(
        self,
        *,
        type: str,
        session_id: str,
        payload: dict[str, Any],
    ) -> None:
        self.id = _next_event_id()
        self.type = type
        self.session_id = session_id
        self.occurred_at = _utcnow_iso()
        self.payload = payload

    def envelope(self) -> dict[str, Any]:
        """SPEC §7.2 wire envelope."""

        return {
            "type": self.type,
            "occurred_at": self.occurred_at,
            "payload": self.payload,
        }


class EventBus:
    """Per-session asyncio.Queue fan-out.

    Each ``subscribe(session_id)`` returns a fresh queue plumbed
    into the bus until the iterator drops out (consumer disconnects
    / handler returns). ``publish`` writes to every active queue
    for the matching session.

    Concurrency: single-process, single-thread (FastAPI worker).
    The bus itself is a plain dict; queues are asyncio.Queue
    instances which serialize their own operations.
    """

    def __init__(self, *, queue_capacity: int = 256) -> None:
        self._capacity = queue_capacity
        # session_id -> list of subscriber queues
        self._subs: dict[str, list[asyncio.Queue[Event]]] = defaultdict(list)

    def publish(self, event: Event) -> None:
        """Fan-out to every subscriber of event.session_id.

        Drops events when a subscriber's queue is full rather than
        blocking the publisher — slow consumers shouldn't stall the
        agent's turn loop. The dropped events show up as a
        ``server.disposed``-equivalent gap in the client's stream;
        clients catch up via ``GET /v1/sessions/{sid}/messages``
        on reconnect.
        """

        for q in self._subs.get(event.session_id, []):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass

    async def subscribe(
        self, session_id: str
    ) -> AsyncIterator[Event]:
        """Yield events for ``session_id`` until the consumer drops.

        Use as ``async for event in bus.subscribe(sid)``. Cleanup is
        guaranteed via ``finally`` even if the consumer cancels mid-
        iteration.
        """

        q: asyncio.Queue[Event] = asyncio.Queue(maxsize=self._capacity)
        self._subs[session_id].append(q)
        try:
            while True:
                event = await q.get()
                yield event
        finally:
            try:
                self._subs[session_id].remove(q)
            except ValueError:
                pass
            if not self._subs[session_id]:
                del self._subs[session_id]

    def subscriber_count(self, session_id: str) -> int:
        """How many SSE clients are currently attached to ``session_id``.
        Useful for /v1/health diagnostics + tests."""

        return len(self._subs.get(session_id, []))


def heartbeat_payload() -> dict[str, Any]:
    """SPEC §7.1 says emit a heartbeat every 15s so proxies don't
    close idle connections. The payload is empty by design — its
    presence is the signal."""

    return {}


def _ms_since(start: float) -> int:
    return int((time.time() - start) * 1000)
