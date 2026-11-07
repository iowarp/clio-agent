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
  the highway (nothing is derived from an event clio-core does not hold). A failed
  artifact confirm (after the events are held) is reported the same way to the
  sessions that queued one. Every batch is settled whatever raised, and the writer
  thread survives it, so a barrier never waits on a batch that was dropped.
* **Turn end.** The turn's publication reports an unconfirmed record and goes on
  (:func:`flush_session_reported`): losing a tool or routing record never abandons
  the transcript. The ReAct loop's barrier before a model request still raises. The
  wait that keeps a synchronous event after the session's queued ones only waits: it
  leaves a lost group to the next reporting barrier.
* **Thread.** The writer thread is process-lived (``mark_native_thread``), so its
  clio-core calls run on it instead of being handed to a native-pool worker.
* **Shutdown.** The lifespan closes the app's writer, bounded, after the turns settle
  and before clio-core is released (:func:`close_app_writer`); what it could not
  record is logged ``highway_close_unrecorded``. A session's bookkeeping is dropped
  once nothing of it is pending or unreported.

``arc.highway_deferred`` (default true) turns this off: every event is recorded on
the caller's thread as before.
"""

from __future__ import annotations

import contextvars
import logging
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import partial
from typing import Any

from clio_agent.arc.native_threads import mark_native_thread

logger = logging.getLogger(__name__)

__all__ = [
    "DEFERRED_EVENT_TYPES",
    "HIGHWAY_CLOSE_UNRECORDED",
    "HIGHWAY_RECORD_UNCONFIRMED",
    "HighwayRecordUnconfirmed",
    "HighwayWriter",
    "close_app_writer",
    "defer_enabled",
    "defer_event",
    "flush_session",
    "flush_session_reported",
    "highway_writer",
    "operation_queue",
]

#: Typed reason: a deferred group of highway events was not confirmed by clio-core.
HIGHWAY_RECORD_UNCONFIRMED = "highway_record_unconfirmed"

#: Typed reason: the writer closed with queued highway events it did not record.
HIGHWAY_CLOSE_UNRECORDED = "highway_close_unrecorded"

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
    event: Any  # ``None`` for an operation
    after: Callable[[], None] | None
    context: contextvars.Context
    operation: Callable[[], None] | None = None


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
            self._ensure_thread()
            self._wake.notify()

    def submit_operation(self, session_id: str, operation: Callable[[], None]) -> None:
        """Queue a store write that must land before the events queued after it.

        The artifact index puts of a transform go here, ahead of the
        ``artifact.transform.recorded`` event that confirms them.
        """
        with self._lock:
            if self._closed:
                raise RuntimeError("The highway writer is closed")
            state = self._sessions.setdefault(session_id, _Session())
            with state.done:
                state.pending += 1
            context = contextvars.copy_context()
            self._queue.append(_Queued(session_id, None, None, context, operation))
            self._ensure_thread()
            self._wake.notify()

    def _ensure_thread(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(
                target=self._run, name="clio-highway-writer", daemon=True
            )
            self._thread.start()

    def flush(self, session_id: str, timeout: float | None = None, *, report: bool = True) -> None:
        """Wait until every event queued for ``session_id`` is recorded.

        Args:
            session_id: The session whose queued events to wait for.
            timeout: Seconds to wait at most (``None``: until recorded).
            report: ``False`` only waits: an unconfirmed group stays to be reported by
                the session's next reporting barrier (an ordering wait is not one).

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
            if not report:
                return
            error, state.error = state.error, None
        if error is not None:
            self._forget_if_idle(session_id, state)  # reported: nothing left to keep
            raise HighwayRecordUnconfirmed(
                f"{HIGHWAY_RECORD_UNCONFIRMED}: highway events of {session_id} were not "
                f"recorded ({type(error).__name__}: {error})"
            ) from error

    def _forget_if_idle(self, session_id: str, state: _Session) -> None:
        with self._lock:  # lock order as in submit: _lock, then the session's done
            with state.done:
                if (
                    state.pending == 0
                    and state.error is None
                    and self._sessions.get(session_id) is state
                ):
                    del self._sessions[session_id]

    def close(self, timeout: float = 30.0) -> int:
        """Record what is queued, then stop the thread; wait at most ``timeout`` seconds.

        Returns:
            How many queued events and operations were not recorded: still queued or
            in flight when ``timeout`` elapsed, or lost in a group clio-core did not
            confirm and not yet reported at a barrier. Nonzero is logged with the typed
            reason ``highway_close_unrecorded``.
        """
        with self._lock:
            self._closed = True
            self._wake.notify()
            thread = self._thread
        if thread is not None:
            thread.join(timeout)
        with self._lock:
            sessions = dict(self._sessions)
        left = 0
        for state in sessions.values():
            with state.done:
                left += state.pending
        unreported = sorted(sid for sid, state in sessions.items() if state.error is not None)
        if left or unreported:
            logger.warning(
                "%s pending=%d unconfirmed_sessions=%s timeout_s=%s",
                HIGHWAY_CLOSE_UNRECORDED,
                left,
                unreported,
                timeout,
            )
        return left

    def _run(self) -> None:
        mark_native_thread()  # process-lived: its clio-core calls run here, not handed off
        while True:
            with self._lock:
                while not self._queue and not self._closed:
                    self._wake.wait()
                if not self._queue:
                    return
                batch = [self._queue.popleft() for _ in range(min(len(self._queue), _GROUP_LIMIT))]
            self._record(batch)

    def _record(self, batch: list[_Queued]) -> None:
        """Record ``batch``, project it, then settle it -- whatever raised on the way.

        The settle is in a ``finally``: a batch that is never settled leaves its
        sessions' :meth:`flush` waiting forever, and an exception that escaped here
        would end the one writer thread with nothing left to record later batches.
        """
        errors: dict[str, BaseException] = {}
        try:
            error = self._write(batch)
            if error is not None:
                errors = dict.fromkeys((item.session_id for item in batch), error)
            else:
                errors = self._project(batch)
        except BaseException as exc:  # noqa: BLE001 - forwards: the session's next flush raises it
            errors = dict.fromkeys((item.session_id for item in batch), exc)
            _log_unconfirmed(batch, exc)
        finally:
            self._settle(batch, errors)

    def _write(self, batch: list[_Queued]) -> BaseException | None:
        """Put the batch's operations and events in clio-core; the error, if not held."""
        from clio_agent.arc.segment_group import segment_group  # noqa: PLC0415
        from clio_agent.gact.artifacts.index_puts import IndexPut, put_batch  # noqa: PLC0415

        try:
            with segment_group(self._arc._segments, lambda _scope: (0,)) as group:
                index_puts: list[IndexPut] = []  # sent together, before the group's events
                for item in batch:
                    if isinstance(item.operation, IndexPut):
                        index_puts.append(item.operation)
                    elif item.operation is not None:
                        put_batch(index_puts)  # an operation of another kind keeps its place
                        index_puts = []
                        item.context.run(item.operation)  # put now: before the group's events
                    else:  # staged: the group's events reach clio-core at its flush
                        item.context.run(self._arc.on_semantic_event, item.event)
                put_batch(index_puts)
                group.flush()
        except Exception as exc:  # noqa: BLE001 - forwards: the session's next flush raises it
            _log_unconfirmed(batch, exc)
            return exc
        return None

    def _project(self, batch: list[_Queued]) -> dict[str, BaseException]:
        """Send held events to the highway, then their confirms; errors by session."""
        from clio_agent.gact.artifacts.index_puts import (  # noqa: PLC0415
            IndexPut,
            collecting,
            put_batch,
        )

        sink = getattr(self._arc, "_highway_sink", None)
        confirms: list[IndexPut] = []  # the turn's confirm puts, sent together after
        confirming: set[str] = set()
        for item in batch:
            if item.operation is not None:
                continue
            try:
                if sink is not None:
                    item.context.run(sink, item.event)
                if item.after is not None:
                    confirming.add(item.session_id)
                    item.context.run(collecting, confirms, item.after)
            except Exception:  # noqa: BLE001 - the record stands; the projection is logged
                logger.exception(
                    "highway projection failed after record event=%s session=%s",
                    getattr(item.event, "event_type", ""),
                    item.session_id,
                )
        try:
            put_batch(confirms)
        except Exception as exc:  # noqa: BLE001 - forwards: those sessions' next flush raises it
            # The events are held; their artifact confirms are not. The sessions that
            # queued a confirm learn it at their next barrier, as for a lost group.
            _log_unconfirmed([item for item in batch if item.session_id in confirming], exc)
            return dict.fromkeys(confirming, exc)
        return {}

    def _settle(self, batch: list[_Queued], errors: dict[str, BaseException]) -> None:
        """Count the batch done per session; forget a session with nothing left to report.

        Lock order matches :meth:`submit` (``_lock``, then the session's ``done``), so a
        session is dropped only while no submit can be adding to it.
        """
        counts: dict[str, int] = {}
        for item in batch:
            counts[item.session_id] = counts.get(item.session_id, 0) + 1
        with self._lock:
            for sid, count in counts.items():
                state = self._sessions.get(sid)
                if state is None:  # pragma: no cover - a queued session is never dropped
                    continue
                with state.done:
                    state.pending -= count
                    error = errors.get(sid)
                    if error is not None and state.error is None:
                        state.error = error
                    if state.pending == 0 and state.error is None:
                        del self._sessions[sid]
                    state.done.notify_all()


def _log_unconfirmed(batch: list[_Queued], exc: BaseException) -> None:
    logger.warning(
        "%s events=%d sessions=%s error=%r",
        HIGHWAY_RECORD_UNCONFIRMED,
        len(batch),
        sorted({item.session_id for item in batch}),
        exc,
    )


def highway_writer(app: Any, arc: Any) -> HighwayWriter | None:
    """The app's writer over ``arc`` (created on first use), or ``None`` when off.

    When the app's ARC changed, the writer over the old one is drained and closed
    first (bounded, :meth:`HighwayWriter.close`): its queued events reach the ARC they
    were emitted to, and its thread does not outlive it. Until then the app keeps it,
    so a session's :func:`flush_session` still waits for what it queued there. After
    the app's writer is closed at shutdown, ``None``: a late event is recorded on the
    caller's thread, as with ``arc.highway_deferred`` off.
    """
    state = getattr(app, "state", None)
    if state is None or not defer_enabled() or not hasattr(arc, "_segments"):
        return None
    writer = getattr(state, "highway_writer", None)
    if writer is not None and writer._arc is arc and writer._closed:
        return None
    if writer is None or writer._arc is not arc:
        with _CREATE:
            writer = getattr(state, "highway_writer", None)
            if writer is None or writer._arc is not arc:
                if writer is not None:
                    writer.close(_SWAP_CLOSE_TIMEOUT_S)
                writer = HighwayWriter(arc)
                state.highway_writer = writer
    return writer


_CREATE = threading.Lock()

#: Bound on draining a replaced writer, and on the app's writer at shutdown (seconds).
_SWAP_CLOSE_TIMEOUT_S = 30.0
_SHUTDOWN_CLOSE_TIMEOUT_S = 10.0


def close_app_writer(app: Any, timeout: float = _SHUTDOWN_CLOSE_TIMEOUT_S) -> int:
    """Record the app's queued highway events and stop its writer (shutdown, bounded).

    The lifespan calls it once the turns have settled and before clio-core is released,
    so the last turn's tool and model records reach clio-core instead of dying with the
    writer's daemon thread. Returns how many were not recorded (logged typed by
    :meth:`HighwayWriter.close`); without a writer, 0.
    """
    writer = getattr(getattr(app, "state", None), "highway_writer", None)
    if writer is None:
        return 0
    return writer.close(timeout)


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
        # Only an ordering wait: a lost group stays for the ReAct loop's barrier (or the
        # turn's publication) to report, once, instead of failing this unrelated event.
        flush_session(app, session_id, report=False)
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


def operation_queue(app: Any, session_id: str) -> Callable[[Callable[[], None]], None] | None:
    """Queue store writes on the session's ordered writer, or ``None`` to write through."""
    arc = getattr(getattr(app, "state", None), "arc", None)
    if not session_id or arc is None:
        return None
    writer = highway_writer(app, arc)
    if writer is None:
        return None
    return partial(writer.submit_operation, session_id)


def flush_session_reported(app: Any, session_id: str, step: str) -> bool:
    """The turn-publication barrier: an unconfirmed record is logged typed, not raised.

    The record of a tool call or a routing decision is observability; the turn's
    transcript is the reply. Every publication step of the turn uses this one policy
    (``turn_finalize``), so losing such a record never abandons the transcript.

    Returns:
        ``False`` when the session's queued records were not all confirmed.
    """
    try:
        flush_session(app, session_id)
    except HighwayRecordUnconfirmed as exc:
        logger.warning(
            "%s step=%s session=%s error=%s", HIGHWAY_RECORD_UNCONFIRMED, step, session_id, exc
        )
        return False
    return True


def flush_session(
    app: Any, session_id: str, timeout: float | None = None, *, report: bool = True
) -> None:
    """Barrier: return once ``session_id``'s queued highway events are recorded.

    Raises :class:`HighwayRecordUnconfirmed` for a lost group unless ``report`` is
    ``False`` (:meth:`HighwayWriter.flush`).
    """
    writer = getattr(getattr(app, "state", None), "highway_writer", None)
    if writer is not None and session_id:
        from clio_agent.runtime import io_audit  # noqa: PLC0415

        started = time.perf_counter()
        writer.flush(session_id, timeout=timeout, report=report)
        io_audit.barrier(session_id, time.perf_counter() - started)
