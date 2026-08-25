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
import logging
import time
from collections import defaultdict
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any, AsyncIterator

from clio_agent.runtime.stream_audit import stream_audit

logger = logging.getLogger(__name__)


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_decade_boundary(n: int) -> bool:
    """True for ``n`` in ``{1, 10, 100, 1000, ...}`` (string-based — no float
    precision hazard). Used to throttle the ``bus.queue_full`` warning log
    rate to a decade cadence (#1214 D1)."""

    if n < 1:
        return False
    digits = str(n)
    return digits[0] == "1" and set(digits[1:]) <= {"0"}


_event_id_counter = itertools.count(1)


def _next_event_id() -> int:
    """Monotonic id used as the SSE ``id:`` line so clients can
    resume via ``Last-Event-ID`` after a reconnect (SPEC §7.1)."""

    return next(_event_id_counter)


class Event:
    """In-memory event record."""

    __slots__ = ("id", "type", "session_id", "occurred_at", "payload", "replay", "transient")

    def __init__(
        self,
        *,
        type: str,
        session_id: str,
        payload: dict[str, Any],
        replay: bool = False,
        transient: bool = False,
    ) -> None:
        self.id = _next_event_id()
        self.type = type
        self.session_id = session_id
        self.occurred_at = _utcnow_iso()
        self.payload = payload
        self.replay = replay
        # Transient events are connection plumbing (server.heartbeat
        # keepalives), not session timeline: they are delivered to live
        # subscribers only — never recorded into the replay history and
        # never counted as turn progress (iowarp/clio-agent#761).
        self.transient = transient

    def replay_copy(self) -> "Event":
        """Return a replay-marked copy preserving the original event id/time."""

        copy = object.__new__(Event)
        copy.id = self.id
        copy.type = self.type
        copy.session_id = self.session_id
        copy.occurred_at = self.occurred_at
        copy.payload = self.payload
        copy.replay = True
        copy.transient = self.transient
        return copy

    def envelope(self) -> dict[str, Any]:
        """SPEC §7.2 wire envelope."""

        envelope: dict[str, Any] = {
            "type": self.type,
            "occurred_at": self.occurred_at,
            "payload": self.payload,
        }
        if self.replay:
            envelope["replay"] = True
        return envelope


class EventBus:
    """Per-session asyncio.Queue fan-out.

    Each ``subscribe(session_id)`` returns a fresh queue plumbed
    into the bus until the iterator drops out (consumer disconnects
    / handler returns). ``publish`` writes to every active queue
    for the matching session.

    Concurrency: single-process, multi-producer. The subscriber
    queues are ``asyncio.Queue`` instances owned by the server's
    event loop, and ``asyncio.Queue`` is NOT thread-safe — but
    ``publish`` is called from worker threads (the MCP tool-observer
    thread, the LM-bind thread running its own loop, executor
    threads emitting semantic events). The bus therefore binds the
    owning loop on first ``subscribe`` and bridges every foreign-
    thread publish onto it via ``loop.call_soon_threadsafe``, so
    ``_subs``/``_history`` and the queues are only ever mutated on
    the owning loop (iowarp/clio-agent#758). Before a loop is bound
    there are no subscriber queues, so publishes touch only the
    replay history and run inline.
    """

    def __init__(self, *, queue_capacity: int = 256, history_per_session: int = 256) -> None:
        self._capacity = queue_capacity
        self._history_cap = history_per_session
        # session_id -> list of subscriber queues
        self._subs: dict[str, list[asyncio.Queue[Event]]] = defaultdict(list)
        # session_id -> bounded replay log. New subscribers receive
        # ``history[last_event_id + 1:]`` on connect so they don't
        # miss events published before they arrived (SPEC §7.3 replay).
        self._history: dict[str, list[Event]] = defaultdict(list)
        # Highest non-transient timeline id accepted by this bus instance.
        # Unlike the module-global id generator this starts at zero with every
        # service instance, allowing the SSE route to recognize a resume cursor
        # that belongs to a process which has restarted.
        self._highest_event_id = 0
        # session_id -> last publish wall (time.monotonic) timestamp. Every
        # progress signal a turn makes (semantic events, message deltas, tool
        # parts) flows through ``publish``, so this doubles as a per-session
        # liveness heartbeat the turn watchdog reads to distinguish a slow-but-
        # progressing turn from a wedged one. Plain float assignment is safe to
        # set from worker threads (the agent loop runs in an executor).
        self._last_publish_monotonic: dict[str, float] = {}
        # The event loop that owns _subs/_history and every subscriber
        # queue. Bound by the first subscribe(); foreign-thread publishes
        # are bridged onto it via call_soon_threadsafe (#758).
        self._loop: asyncio.AbstractEventLoop | None = None
        # session_id -> cumulative subscriber-queue-full drop count (#1214: no
        # silent ``except asyncio.QueueFull: pass``). Read via ``dropped_total``.
        self._dropped_total: dict[str, int] = defaultdict(int)
        # session_id -> whether the session is currently mid drop-streak.
        # Set on every drop; cleared only when a FULL _deliver() cycle for
        # the session drops nothing (every live subscriber queue for it
        # accepted the event) -- gates the ONE recovery logger.info line in
        # _note_delivery_recovered. The warning-log RATE itself is decade-
        # boundary gated in _record_drop, independent of this flag (#1214 D1).
        self._drop_burst_active: dict[str, bool] = defaultdict(bool)

    def publish(self, event: Event) -> None:
        """Fan-out to every subscriber of event.session_id + record
        into the replay log.

        Drops events into live queues when a subscriber's queue is
        full rather than blocking the publisher — slow consumers
        shouldn't stall the agent's turn loop. This is silent ON THE
        WIRE: no gap-marker SSE event is emitted to the client (an
        earlier version of this docstring claimed a ``server.disposed``-
        equivalent gap event fired here; no such event was ever emitted
        — iowarp/clio-agent#1214), so an attached client reading its
        live stream simply never sees the dropped event.

        ``Last-Event-ID`` resume does NOT reliably recover a drop either
        (a second docstring inaccuracy this corrects — #1214 review): resume
        replays ``history[id > last_event_id]`` — everything STRICTLY NEWER
        than the client's own watermark. A client that stays connected and
        keeps receiving LATER events after a drop never reconnects at all
        (there is nothing to resume), and a client that DOES reconnect has
        by then already advanced its watermark past the dropped id via
        those later events — so the hole sits BELOW the watermark, exactly
        where resume cannot look. Verified: delivered ``[2, 5]`` (3 and 4
        dropped mid-stream), resume from ``last_event_id=5`` yields only
        ``[6, ...]`` — 3 and 4 are gone for good via this path. The one
        real recovery channel is ``GET /v1/sessions/{sid}/messages``, and
        only for MESSAGE PARTS: the persisted transcript/message store
        records parts independently of whether their SSE delivery
        succeeded, so a full re-fetch shows them regardless. Anything NOT
        projected into that message/parts model — ``tool.call.*``,
        semantic/trace events — has NO recovery path at all once dropped.

        Every drop IS accounted for: a per-session counter
        (:meth:`dropped_total`), a decade-boundary-throttled
        ``logger.warning`` (1, 10, 100, ... drops), and a ``bus.queue_full``
        stream-audit row per drop (#1214) — never a bare ``except
        asyncio.QueueFull: pass``.

        Safe to call from any thread: publishes arriving off the
        owning loop are bridged via ``loop.call_soon_threadsafe``
        (iowarp/clio-agent#758).
        """

        # Record the liveness heartbeat for the turn watchdog, attributed
        # strictly to the event's own session: folding every publish into a
        # global key made the no-progress watchdog inert as soon as a second
        # session was active (iowarp/clio-agent#761). Transient keepalives
        # (server.heartbeat) fire on a timer for any attached SSE client, so
        # they are NOT progress either. Stamped in the publishing thread
        # (plain float assignment is GIL-atomic) so liveness is visible even
        # before the owning loop runs a bridged callback.
        if not event.transient:
            self._last_publish_monotonic[event.session_id] = time.monotonic()

        try:
            running: asyncio.AbstractEventLoop | None = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        owner = self._loop
        if owner is None or running is owner:
            # On the owning loop — or no loop bound yet, in which case no
            # subscriber queues exist and this only appends replay history.
            self._deliver(event)
            return
        # Foreign thread (or a coroutine on a different loop, e.g. the
        # LM-bind worker's private loop): bridge onto the owning loop so
        # queue/history mutations stay single-threaded and waiting getters
        # are woken reliably.
        try:
            owner.call_soon_threadsafe(self._deliver, event)
        except RuntimeError as exc:
            # Degraded path: the owning loop is closed (server teardown).
            # Live delivery is impossible; keep the replay record so a
            # reconnecting client can still catch up, and say so.
            logger.warning(
                "eventbus_publish_fallback reason=owner_loop_closed "
                "event_type=%s session_id=%s live_delivery=dropped error=%s",
                event.type,
                event.session_id,
                exc,
            )
            if not event.transient:
                self._record_history(event)

    def _record_history(self, event: Event) -> None:
        """Append to the bounded per-session replay log."""

        self._highest_event_id = max(self._highest_event_id, event.id)
        log = self._history[event.session_id]
        log.append(event)
        if len(log) > self._history_cap:
            del log[: len(log) - self._history_cap]

    def _deliver(self, event: Event) -> None:
        """Record replay history + fan out to live subscriber queues.

        Runs on the owning loop once one is bound (``publish`` bridges
        foreign-thread callers); before a loop is bound there are no
        subscriber queues, so running inline is safe.

        Transient events (server.heartbeat keepalives) skip the replay
        history: recording one every 15s per subscriber evicts every
        real event from the bounded buffer within an idle hour, gapping
        ``Last-Event-ID`` resume (iowarp/clio-agent#761).
        """

        if not event.transient:
            self._record_history(event)
        subscriber_sessions = list(self._subs) if event.session_id == "" else [event.session_id]
        for subscriber_session in subscriber_sessions:
            dropped_this_delivery = False
            for q in self._subs.get(subscriber_session, []):
                try:
                    q.put_nowait(event)
                except asyncio.QueueFull:
                    self._record_drop(subscriber_session, event.type)
                    dropped_this_delivery = True
            if not dropped_this_delivery:
                self._note_delivery_recovered(subscriber_session)

    def _record_drop(self, session_id: str, event_type: str) -> None:
        """Typed accounting for one subscriber-queue-full drop (#1214).

        Replaces a bare ``except asyncio.QueueFull: pass``: every drop
        increments :attr:`_dropped_total` for ``session_id`` and emits a
        ``bus.queue_full`` stream-audit row (gated behind
        ``CLIO_STREAM_AUDIT_LOG``, zero cost when off) so the full drop
        history is queryable after the fact — the ``stream_fallback``
        typed-reason convention (RULE: no silent fallback).

        ``logger.warning`` fires at DECADE boundaries of the cumulative
        count (1, 10, 100, 1000, ...) rather than once per "burst". A
        burst-boundary design was tried first and degenerated under real
        interleaving: resetting the "announced" flag on every clean
        delivery meant a slow-but-draining consumer (drop, catch up, drop,
        catch up, ...) re-armed the warning almost every cycle (39 warnings
        for 77 drops in review), and two subscribers on one session — one
        stuck, one healthy — reset the flag on the healthy queue's success
        even while the stuck queue kept failing (18 warnings for 18 drops).
        Decade escalation is immune to interleaving noise (it only depends
        on the monotonic cumulative total) while still yielding LESS log
        volume as a burst grows, never more. Every drop still gets its own
        audit row regardless of whether it crossed a decade boundary — the
        counter and the audit trail stay exact either way; only the log
        RATE is throttled.
        """

        total = self._dropped_total[session_id] + 1
        self._dropped_total[session_id] = total
        self._drop_burst_active[session_id] = True
        if _is_decade_boundary(total):
            logger.warning(
                "eventbus_queue_full session_id=%s event_type=%s dropped_total=%d",
                session_id,
                event_type,
                total,
            )
        stream_audit(
            "bus.queue_full",
            session_id=session_id,
            event_type=event_type,
            dropped_total=total,
        )

    def _note_delivery_recovered(self, session_id: str) -> None:
        """Log ONE ``INFO`` line the first time a session's drop streak ends.

        Evaluated once per :meth:`_deliver` call (not per subscriber queue),
        so a multi-subscriber session only "recovers" when EVERY live queue
        for it accepted this delivery — a healthy queue's success no longer
        masks a stuck sibling queue's ongoing drops (the exact bug the
        per-queue reset in :meth:`_record_drop`'s prior design had). A no-op
        when the session was not mid-streak, so this never logs on the
        common all-clear path.
        """

        if self._drop_burst_active.get(session_id):
            self._drop_burst_active[session_id] = False
            logger.info(
                "eventbus_queue_full_recovered session_id=%s dropped_total=%d",
                session_id,
                self._dropped_total.get(session_id, 0),
            )

    def dropped_total(self, session_id: str) -> int:
        """Cumulative subscriber-queue-full drops for ``session_id``.

        ``0`` when nothing has ever been dropped. Backed by the same
        counter :meth:`_record_drop` increments and the ``bus.queue_full``
        stream-audit rows report — read by tests/diagnostics, not the wire
        (#1214: this is the observability half; a wire-visible gap marker
        is a separate, spec-first follow-up)."""

        return self._dropped_total.get(session_id, 0)

    def last_publish_monotonic(self, session_id: str) -> float:
        """Return the ``time.monotonic`` of the most recent publish for a session.

        Returns ``0.0`` if nothing has ever been published for the session.
        Used by the per-turn no-progress watchdog: as long as the turn keeps
        publishing events (semantic spans, tool parts, message deltas) the
        turn is making progress and must not be aborted, regardless of total
        wall-clock elapsed. Only a stretch with no published event for longer
        than the configured window counts as a wedged turn.
        """

        return self._last_publish_monotonic.get(session_id, 0.0)

    def _history_snapshot(self, session_id: str) -> list[Event]:
        """Return global plus session history ordered by event id."""

        if session_id == "":
            return list(self._history.get("", []))
        return sorted(
            [
                *self._history.get("", []),
                *self._history.get(session_id, []),
            ],
            key=lambda event: event.id,
        )

    async def subscribe(self, session_id: str, *, last_event_id: int = 0) -> AsyncIterator[Event]:
        """Yield events for ``session_id`` until the consumer drops.

        ``last_event_id`` is the highest event id the client already
        has (from the ``Last-Event-ID`` header or the SSE ``id:``
        line); the bus first drains any buffered events strictly
        newer than that, then streams live.

        Use as ``async for event in bus.subscribe(sid)``. Cleanup is
        guaranteed via ``finally`` even if the consumer cancels
        mid-iteration.
        """

        # Bind the owning loop on first subscribe: every queue the bus
        # fans out to is consumed on this loop, so this is the loop that
        # foreign-thread publishes must be bridged onto (#758).
        loop = asyncio.get_running_loop()
        if self._loop is None:
            self._loop = loop
        elif loop is not self._loop:
            # Never expected in this app (one serving loop); surfaced
            # loudly because cross-loop queues cannot be woken safely.
            logger.warning(
                "eventbus_subscribe_fallback reason=foreign_loop_subscriber "
                "session_id=%s — subscriber runs on a different event loop "
                "than the bus owner; delivery to it is not thread-safe",
                session_id,
            )
        q: asyncio.Queue[Event] = asyncio.Queue(maxsize=self._capacity)
        self._subs[session_id].append(q)
        try:
            # Replay the buffered tail first. This matters for the
            # happy path too: the TUI creates a session, immediately
            # POSTs a message, and only then subscribes to SSE —
            # without replay it misses every message.* event from the
            # turn that just fired. We snapshot the history up-front
            # so events published DURING replay come via the queue
            # only (not also via the snapshot), avoiding duplicates.
            snapshot = self._history_snapshot(session_id)
            replayed_max = last_event_id
            for ev in snapshot:
                if ev.id > last_event_id:
                    yield ev.replay_copy()
                    if ev.id > replayed_max:
                        replayed_max = ev.id
            while True:
                event = await q.get()
                if event.id <= replayed_max:
                    continue
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

    @property
    def history_capacity(self) -> int:
        """Maximum replay events retained for each session scope."""

        return self._history_cap

    @property
    def highest_event_id(self) -> int:
        """Highest timeline cursor published by this service instance."""

        return self._highest_event_id

    def latest_event_id(self, session_id: str) -> int:
        """Return the newest cursor visible on a session-scoped SSE feed."""

        return max((event.id for event in self._history_snapshot(session_id)), default=0)

    def session_events_since(self, session_id: str, *, cursor: int = 1) -> list["Event"]:
        """Return ``session_id``'s recorded events with ``id >= cursor``, ordered by id.

        Reads the SAME bounded per-session replay history the SSE feed at
        ``GET /v1/sessions/{sid}/events`` replays (``subscribe`` drains
        ``history[last_event_id + 1:]``), so an incremental reader — the
        spawn-runtime ``observe_agent_tasks`` tool watching a child's progress
        (iowarp/clio-agent#1000) — resumes from a monotonic cursor without missing
        or re-reading events. It is a READ over the existing store, NOT a fifth
        history (RULE 4).

        The event ``id`` is a process-global monotonic counter (``_event_id_counter``),
        so a cursor read against one session is coherent with the same counter used
        across sessions; ``next_cursor`` is simply ``events[-1].id + 1``.

        Thread-safe for a worker-thread caller (the observe tool runs off the owning
        loop): the per-session history lists are only ever APPENDED to (never
        reordered) on the owning loop, and a list slice is atomic under the GIL, so
        the slice copy below is a consistent snapshot even under a concurrent append.
        Bounded like the SSE replay: an event evicted from the ``history_per_session``
        buffer is not returned (a very chatty child can outrun the buffer — the same
        bound the SSE ``Last-Event-ID`` resume already carries).
        """

        events: list[Event] = []
        keys = ("", session_id) if session_id else ("",)
        for key in keys:
            events.extend(self._history.get(key, ())[:])
        events.sort(key=lambda event: event.id)
        return [event for event in events if event.id >= cursor]


def heartbeat_payload() -> dict[str, Any]:
    """SPEC §7.1 says emit a heartbeat every 15s so proxies don't
    close idle connections. The payload is empty by design — its
    presence is the signal."""

    return {}


def heartbeat_event(session_id: str) -> Event:
    """Build the 15-s SSE keepalive for ``session_id``.

    Marked transient: a heartbeat is connection plumbing, not part of
    the session's event timeline, so it is delivered to live
    subscribers only — it never enters the replay history (it would
    evict real events from the bounded buffer) and never counts as
    turn progress for the no-progress watchdog
    (iowarp/clio-agent#761).
    """

    return Event(
        type="server.heartbeat",
        session_id=session_id,
        payload=heartbeat_payload(),
        transient=True,
    )


def _ms_since(start: float) -> int:
    return int((time.time() - start) * 1000)


def _publish_transcript_event(
    bus: EventBus,
    sid: str,
    event_type: str,
    payload: Mapping[str, Any],
) -> None:
    """Publish one normalized transcript event alongside legacy message events."""

    bus.publish(Event(type=event_type, session_id=sid, payload=dict(payload)))
