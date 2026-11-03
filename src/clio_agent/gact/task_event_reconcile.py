"""Retrying a task record that may have partly landed: each event once in each log.

A task event is written to two records in order: the task-event lane (what task trees
read) and then the canonical ``_events`` log (trace, export, provenance, the lane's
own migration) with its highway emission (:func:`task_event_lane.record`). A write
can fail between them. A group commit (:mod:`clio_agent.gact.task_history_group`)
widens that window: it puts every queued record's lane appends in one rank and the
``_events`` appends in the next, so a failure of the later rank leaves the lane
holding records the log does not, for several operations at once. Each failed
operation is then retried alone by recovery.

A retry that re-runs the operation as if nothing had landed appended the lane records
again (a second ``task.submission`` and a second settled snapshot), or was refused
because the settled outcome it re-derived (a new completion time) differed from the
one the lane already held; then the ``_events`` copy was never written and the outcome
was reported unrecorded although task readers showed it.

Under :func:`reconciled` a retry is idempotent by event identity. The operation must
re-derive the same events (same identities and records): the caller fixes them when
it first queues the operation. Each event (type and identity) is then written only to
the record that lacks it, and the highway emission goes with the ``_events`` write.
A record that already holds the identity with a different body is a conflict, never
overwritten. Every completion is logged with the typed reason ``task_event_reconciled``.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, TypeVar

if TYPE_CHECKING:
    from clio_agent.gact.task_history import TaskHistory

__all__ = [
    "TASK_EVENT_CONFLICT",
    "TASK_EVENT_RECONCILED",
    "TaskEventConflict",
    "active",
    "missing",
    "reconciled",
]

logger = logging.getLogger(__name__)

#: Typed reason: a retried task event was written only to the record that lacked it.
TASK_EVENT_RECONCILED = "task_event_reconciled"
#: Typed reason: a retried task event's identity holds a different record.
TASK_EVENT_CONFLICT = "task_event_identity_conflict"

T = TypeVar("T")
_ACTIVE = threading.local()


class TaskEventConflict(ValueError):
    """A retried task event's identity is already recorded with a different body."""

    reason = TASK_EVENT_CONFLICT


def active(history: TaskHistory) -> bool:
    """Whether this thread is retrying an operation of ``history`` under :func:`reconciled`."""
    return getattr(_ACTIVE, "history", None) is history


@contextmanager
def _reconciling(history: TaskHistory) -> Iterator[None]:
    previous = getattr(_ACTIVE, "history", None)
    _ACTIVE.history = history
    try:
        yield
    finally:
        _ACTIVE.history = previous


def reconciled(history: TaskHistory, write: Callable[[], T]) -> T:
    """Run ``write`` (on the history writer) as a retry: each task event once per record."""
    with _reconciling(history):
        return write()


def _held(contents: Iterator[Any], event_type: str, identity: str, record: Any) -> bool:
    for content in contents:
        if not isinstance(content, dict) or content.get("event_type") != event_type:
            continue
        payload = content.get("payload") or {}
        if payload.get("observation_id") != identity:
            continue
        if payload.get("record") != record:
            raise TaskEventConflict(
                f"{TASK_EVENT_CONFLICT}: {event_type} {identity} holds a different record"
            )
        return True
    return False


def missing(history: TaskHistory, event: Any) -> tuple[bool, bool]:
    """Which of the lane and the ``_events`` log lack ``event`` (its type and identity).

    Reads the session's lane and its ``_events`` log from clio-core (or the cache of
    what clio-core confirmed). Only a retry pays for these reads.

    Raises:
        TaskEventConflict: A record holds the identity with a different body.
    """
    from clio_agent.gact.task_event_lane import session_task_events  # noqa: PLC0415 - cycle

    arc = history.arc
    session_id = str(event.session_id)
    event_type, payload = str(event.event_type), event.payload
    identity, record = str(payload["observation_id"]), payload["record"]
    lane = (content for _session, content in session_task_events(arc, session_id))
    log = (segment.content for segment in arc._live.iter_session_event_segments(session_id))
    to_lane = not _held(lane, event_type, identity, record)
    to_log = not _held(log, event_type, identity, record)
    if not (to_lane and to_log):
        logger.warning(
            "Task boundary reason=%s session=%s event=%s identity=%s lane=%s log=%s",
            TASK_EVENT_RECONCILED,
            session_id,
            event_type,
            identity,
            "written" if to_lane else "held",
            "written" if to_log else "held",
        )
    return to_lane, to_log
