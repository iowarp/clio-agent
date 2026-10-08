"""In-memory event timeline of each infrastructure operation (progress + live log).

Every operation gets its own monotonic event ids (from 1). The runtime
publishes ``operation.progress`` (steps/current step/reuse), ``operation.log``
(one redacted output line), ``operation.reuse`` and finally
``operation.completed``; the SSE route replays what a client has not seen
(``Last-Event-ID`` is the highest id it holds, resume is exclusive, like
``GET /v1/sessions/{sid}/events``) and then follows live until the operation
completes.

The timeline is the live log's source of truth, independent of the bounded
``logs`` text on the durable operation record (and of the executor's 16 kB
output tail): lines are published as the command produces them. It keeps the
last ``max_events`` events per operation and the newest ``max_operations``
operations; a client whose cursor fell out of that window gets a
``stream.gap`` first.
"""

from __future__ import annotations

import asyncio
import json
from collections import OrderedDict, deque
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

TERMINAL_EVENT = "operation.completed"
LOG_EVENT = "operation.log"
HEARTBEAT_SECONDS = 15.0


@dataclass(frozen=True)
class OperationEvent:
    """One event of an operation's timeline (``id`` 0: connection-only, no SSE id)."""

    id: int
    type: str
    operation_id: str
    payload: dict[str, Any]
    at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def frame(self) -> bytes:
        """The SSE wire frame (``event:``, ``id:`` unless connection-only, ``data:``)."""

        data = {
            "id": self.id,
            "type": self.type,
            "operation_id": self.operation_id,
            "at": self.at,
            "payload": self.payload,
        }
        id_line = f"id: {self.id}\n" if self.id > 0 else ""
        return f"event: {self.type}\n{id_line}data: {json.dumps(data)}\n\n".encode()


@dataclass
class _Timeline:
    events: deque[OperationEvent]
    next_id: int = 1
    closed: bool = False
    waiters: list[asyncio.Future[None]] = field(default_factory=list)

    def wake(self) -> None:
        for waiter in self.waiters:
            if not waiter.done():
                waiter.set_result(None)
        self.waiters.clear()


class OperationEventLog:
    """Bounded per-operation event timelines with live followers."""

    def __init__(self, *, max_events: int = 5000, max_operations: int = 64) -> None:
        self._max_events = max_events
        self._max_operations = max_operations
        self._timelines: OrderedDict[str, _Timeline] = OrderedDict()

    def _timeline(self, operation_id: str) -> _Timeline:
        timeline = self._timelines.get(operation_id)
        if timeline is None:
            timeline = _Timeline(deque(maxlen=self._max_events))
            self._timelines[operation_id] = timeline
            self._evict()
        return timeline

    def _evict(self) -> None:
        while len(self._timelines) > self._max_operations:
            closed = next((key for key, row in self._timelines.items() if row.closed), None)
            if closed is None:
                return
            del self._timelines[closed]

    def known(self, operation_id: str) -> bool:
        """Whether this process holds a timeline for the operation."""

        return operation_id in self._timelines

    def publish(self, operation_id: str, kind: str, payload: dict[str, Any]) -> int:
        """Append one event and wake the operation's followers; returns its id."""

        timeline = self._timeline(operation_id)
        if timeline.closed:
            return timeline.next_id - 1
        event = OperationEvent(timeline.next_id, kind, operation_id, payload)
        timeline.next_id += 1
        timeline.events.append(event)
        if kind == TERMINAL_EVENT:
            timeline.closed = True
        timeline.wake()
        return event.id

    def latest_id(self, operation_id: str) -> int:
        """The newest event id of an operation (0 when none)."""

        timeline = self._timelines.get(operation_id)
        return timeline.next_id - 1 if timeline else 0

    def after(
        self, operation_id: str, cursor: int, *, kinds: frozenset[str] | None = None
    ) -> tuple[list[OperationEvent], bool]:
        """Retained events newer than ``cursor`` and whether older ones were dropped."""

        timeline = self._timelines.get(operation_id)
        if timeline is None:
            return [], False
        events = [
            row
            for row in timeline.events
            if row.id > cursor and (kinds is None or row.type in kinds)
        ]
        first = timeline.events[0].id if timeline.events else timeline.next_id
        return events, cursor < first - 1

    async def follow(
        self, operation_id: str, cursor: int, *, heartbeat: float = HEARTBEAT_SECONDS
    ) -> AsyncIterator[OperationEvent]:
        """Replay events after ``cursor``, then follow live until the operation completes.

        Yields a connection-only ``stream.gap`` when the cursor fell out of the
        retained window, and ``server.heartbeat`` every ``heartbeat`` seconds
        of silence; ends after the terminal event.
        """

        events, gap = self.after(operation_id, cursor)
        if gap:
            first = events[0].id if events else self.latest_id(operation_id) + 1
            yield OperationEvent(
                0,
                "stream.gap",
                operation_id,
                {"reason": "log_window_exceeded", "first_retained_id": first},
            )
        while True:
            for event in events:
                cursor = event.id
                yield event
                if event.type == TERMINAL_EVENT:
                    return
            timeline = self._timeline(operation_id)
            if timeline.closed and cursor >= timeline.next_id - 1:
                return
            waiter: asyncio.Future[None] = asyncio.get_running_loop().create_future()
            timeline.waiters.append(waiter)
            try:
                await asyncio.wait_for(asyncio.shield(waiter), timeout=heartbeat)
            except TimeoutError:
                yield OperationEvent(0, "server.heartbeat", operation_id, {})
            finally:
                if waiter in timeline.waiters:
                    timeline.waiters.remove(waiter)
            events, _ = self.after(operation_id, cursor)


__all__ = ["LOG_EVENT", "TERMINAL_EVENT", "OperationEvent", "OperationEventLog"]
