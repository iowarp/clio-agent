"""Highway-only semantic events recorded behind the call, in groups (owner module).

Some semantic events are read only from the record afterwards — the trace, export,
crash recovery and the live projection — never by the step that produced them:
``tool.call.started`` / ``tool.call.completed``, ``routing.decision``,
``artifact.transform.recorded`` and ``lm.call``. Recording each one synchronously put
one ``_events`` append (a clio-core round trip under the chunk's lock) on the tool
call's path, about three per MCP call, and the ``lm.call`` record on the one
process-wide model loop, where a slow put held every agent's model stream.

:class:`HighwayWriter` takes those records off the caller's thread:

* **Order.** One writer thread per app records queued events in the order they were
  queued; a session's events stay in emission order.
* **Groups.** Each writer turn drains what is queued and appends it inside one
  :func:`~clio_agent.arc.segment_group.segment_group`, so a burst of calls costs one
  put per ``_events`` chunk instead of one per event.
* **ARC stays the source.** An event reaches the highway (trace, SSE, hooks) and an
  ``artifact.*`` event confirms its index record only after clio-core holds it, as on
  the synchronous path (:meth:`ARCMemory.record_semantic_event`).
* **Barrier.** :func:`flush_session` returns once every event queued for a session is
  recorded. The ReAct loop calls it before each model request and the turn calls it at
  its end, so clio-core holds a step's records before the next step starts.
* **Failure.** A group whose records clio-core did not confirm is logged with the
  typed reason ``highway_record_unconfirmed`` and raised from the session's next
  :func:`flush_session` as :class:`HighwayRecordUnconfirmed`; its events never reach
  the highway (nothing is derived from an event clio-core does not hold).

``arc.highway_deferred`` (default true) turns this off: every event is recorded on
the caller's thread as before.
"""

from __future__ import annotations

import contextvars
import logging
import threading
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import partial
from typing import Any

logger = logging.getLogger(__name__)

__all__ = [
    "DEFERRED_EVENT_TYPES",
    "HIGHWAY_RECORD_UNCONFIRMED",
    "HighwayRecordUnconfirmed",
    "HighwayWriter",
    "defer_enabled",
    "defer_event",
    "flush_session",
    "highway_writer",
]

#: Typed reason: a deferred group of highway events was not confirmed by clio-core.
HIGHWAY_RECORD_UNCONFIRMED = "highway_record_unconfirmed"

#: Event types whose record no step reads back: recorded behind the call.
DEFERRED_EVENT_TYPES: frozenset[str] = frozenset(
    {
        "tool.call.started",
        "tool.call.completed",
        "routing.decision",
        "artifact.transform.recorded",
        "lm.call",
    }
)

#: Queued events recorded in one writer turn at most (one group).
_GROUP_LIMIT = 512


class HighwayRecordUnconfirmed(RuntimeError):
    """Deferred highway events of this session were not confirmed by clio-core."""

    reason = HIGHWAY_RECORD_UNCONFIRMED


def defer_enabled() -> bool:
    """Whether highway-only events are recorded behind the call (``arc.highway_deferred``)."""
    from clio_agent import conf  # noqa: PLC0415 - avoid import cycle at module load

    return bool(
        conf.resolve(
            "arc.highway_deferred",
            env="CLIO_ARC_HIGHWAY_DEFERRED",
            default=True,
            cast=conf.as_bool,
        )
    )


@dataclass
class _Queued:
    session_id: str
    event: Any
    after: Callable[[], None] | None
    context: contextvars.Context


@dataclass
class _Session:
    pending: int = 0
    error: BaseException | None = None
    done: threading.Condition = field(default_factory=lambda: threading.Condition(threading.Lock()))


class HighwayWriter:
    """Records queued highway events on one thread, in order, in groups."""

    def __init__(self, arc: Any) -> None:
        self._arc = arc
        self._lock = threading.Lock()
        self._wake = threading.Condition(self._lock)
        self._queue: deque[_Queued] = deque()
        self._sessions: dict[str, _Session] = {}
        self._thread: threading.Thread | None = None
        self._closed = False

    def submit(self, session_id: str, event: Any, after: Callable[[], None] | None = None) -> None:
        """Queue ``event`` for ``session_id``; ``after`` runs once clio-core holds it."""
        with self._lock:
            if self._closed:
                raise RuntimeError("The highway writer is closed")
            state = self._sessions.setdefault(session_id, _Session())
            with state.done:
                state.pending += 1
            self._queue.append(_Queued(session_id, event, after, contextvars.copy_context()))
            if self._thread is None:
                self._thread = threading.Thread(
                    target=self._run, name="clio-highway-writer", daemon=True
                )
                self._thread.start()
            self._wake.notify()

    def flush(self, session_id: str, timeout: float | None = None) -> None:
        """Wait until every event queued for ``session_id`` is recorded.

        Raises:
            HighwayRecordUnconfirmed: an earlier group of this session's events was
                not confirmed by clio-core (reported once).
            TimeoutError: ``timeout`` elapsed first.
        """
        with self._lock:
            state = self._sessions.get(session_id)
        if state is None:
            return
        with state.done:
            if not state.done.wait_for(lambda: state.pending == 0, timeout=timeout):
                raise TimeoutError(f"highway events of {session_id} still pending")
            error, state.error = state.error, None
        if error is not None:
            raise HighwayRecordUnconfirmed(
                f"{HIGHWAY_RECORD_UNCONFIRMED}: highway events of {session_id} were not "
                f"recorded ({type(error).__name__}: {error})"
            ) from error

    def close(self, timeout: float = 30.0) -> None:
        """Record what is queued, then stop the thread."""
        with self._lock:
            self._closed = True
            self._wake.notify()
            thread = self._thread
        if thread is not None:
            thread.join(timeout)

    def _run(self) -> None:
        while True:
            with self._lock:
                while not self._queue and not self._closed:
                    self._wake.wait()
                if not self._queue:
                    return
                batch = [self._queue.popleft() for _ in range(min(len(self._queue), _GROUP_LIMIT))]
            self._record(batch)

    def _record(self, batch: list[_Queued]) -> None:
        from clio_agent.arc.segment_group import segment_group  # noqa: PLC0415

        error: BaseException | None = None
        try:
            with segment_group(self._arc._segments, lambda _scope: (0,)) as group:
                for item in batch:
                    item.context.run(self._arc.on_semantic_event, item.event)
                group.flush()
        except Exception as exc:  # noqa: BLE001 - reported at the session's next flush
            error = exc
            logger.warning(
                "%s events=%d sessions=%s error=%r",
                HIGHWAY_RECORD_UNCONFIRMED,
                len(batch),
                sorted({item.session_id for item in batch}),
                exc,
            )
        if error is None:
            sink = getattr(self._arc, "_highway_sink", None)
            for item in batch:
                try:
                    if sink is not None:
                        item.context.run(sink, item.event)
                    if item.after is not None:
                        item.context.run(item.after)
                except Exception:  # noqa: BLE001 - the record stands; the projection is logged
                    logger.exception(
                        "highway projection failed after record event=%s session=%s",
                        getattr(item.event, "event_type", ""),
                        item.session_id,
                    )
        self._settle(batch, error)

    def _settle(self, batch: list[_Queued], error: BaseException | None) -> None:
        counts: dict[str, int] = {}
        for item in batch:
            counts[item.session_id] = counts.get(item.session_id, 0) + 1
        with self._lock:
            states = {sid: self._sessions[sid] for sid in counts}
        for sid, count in counts.items():
            state = states[sid]
            with state.done:
                state.pending -= count
                if error is not None and state.error is None:
                    state.error = error
                state.done.notify_all()


def highway_writer(app: Any, arc: Any) -> HighwayWriter | None:
    """The app's writer over ``arc`` (created on first use), or ``None`` when off."""
    state = getattr(app, "state", None)
    if state is None or not defer_enabled() or not hasattr(arc, "_segments"):
        return None
    writer = getattr(state, "highway_writer", None)
    if writer is None or writer._arc is not arc:
        with _CREATE:
            writer = getattr(state, "highway_writer", None)
            if writer is None or writer._arc is not arc:
                writer = HighwayWriter(arc)
                state.highway_writer = writer
    return writer


_CREATE = threading.Lock()


def defer_event(app: Any, arc: Any, session_id: str, event: Any, payload: Any) -> bool:
    """Queue a highway-only ``event`` on the app's writer; ``False`` to record it now.

    An ``artifact.*`` event confirms its index record once clio-core holds it, as the
    synchronous path does right after its record. Any other event of a session waits
    for the session's queued ones first, so the session's ``_events`` log keeps the
    order the events were emitted in (with the per-call events queued, that wait falls
    at step boundaries).
    """
    event_type = str(getattr(event, "event_type", "") or "")
    if not session_id:
        return False
    if event_type not in DEFERRED_EVENT_TYPES:
        # Recorded now, after what the session queued: its log keeps emission order.
        flush_session(app, session_id)
        return False
    writer = highway_writer(app, arc)
    if writer is None:
        return False
    after = None
    if event_type.startswith("artifact."):
        from clio_agent.gact.artifacts.registry_store import confirm_artifact_event  # noqa: PLC0415

        after = partial(confirm_artifact_event, app, payload)
    writer.submit(session_id, event, after)
    return True


def flush_session(app: Any, session_id: str, timeout: float | None = None) -> None:
    """Barrier: return once ``session_id``'s queued highway events are recorded."""
    writer = getattr(getattr(app, "state", None), "highway_writer", None)
    if writer is not None and session_id:
        writer.flush(session_id, timeout=timeout)
