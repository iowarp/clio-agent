"""The task-event lane: each session's task events, apart from its whole event log.

A task tree is rebuilt from its member sessions' task events only. Those events
used to be read out of each member's whole ``_events`` log, which also holds the
turn's ``lm.call`` records with full prompts, so opening one flow parent read every
child's transcript-sized log. Task events now also go to their own chunked lane,
``_events/t/log`` (the ``_events/t`` family already holds task content and output),
in the same clio-core segment store; tree loads read only that lane.

* **Write.** :func:`record` is the one writer (``TaskHistory._record``): the lane
  append is confirmed first, then the canonical ``_events`` log and the data highway
  (:meth:`ARCMemory.record_semantic_event`). The ``_events`` log stays complete for
  its other readers (trace, export, provenance, the one-time task migrations).
* **Read.** :func:`session_task_events` is what :func:`task_tree_state.load_tree`
  reads; it never touches ``_events``.
* **Migration.** A store written before the lane has its task events only in
  ``_events``. :func:`migrate_task_event_lane` copies them into the lane once, session
  by session, and writes the ``meta.task_event_lane`` done marker last. It is the
  install/upgrade step's entry point; until that step exists, boot calls
  :func:`ensure_task_event_lane`, which runs it once when the marker is absent and
  logs a typed line either way.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import msgspec

from clio_agent.arc import lane_chunking
from clio_agent.arc.live import EVENTS_SCOPE, build_event_content

if TYPE_CHECKING:
    from clio_agent.arc.memory import ARCMemory
    from clio_agent.arc.schema import Segment
    from clio_agent.gact.task_history import TaskHistory

logger = logging.getLogger(__name__)

#: Base scope of the lane's chunk family (chunk N >= 2 is ``_events/t/log/N``).
TASK_EVENT_LANE = f"{EVENTS_SCOPE}/t/log"
#: Done marker (``task_lookup`` record ``meta.task_event_lane``) of the one-time copy.
LANE_DONE = "task_event_lane"
LANE_VERSION = 1

#: The event types a task tree projects; only these enter the lane.
TASK_EVENTS = frozenset(
    {
        "task.observed",
        "task.submission",
        "task.native_attempt",
        "task.subscription",
        "task.alias",
        "session.mail.accepted",
        "session.mail.updated",
        "session.mail.withdrawn",
        "session.coordination.grant",
        "session.input.prepared",
        "workflow.checkpoint",
    }
)


def _capacity(arc: ARCMemory) -> int:
    return int(getattr(arc, "_events_chunk_segments", 512))


def record(arc: ARCMemory, event: Any) -> Any:
    """Confirm a task event in its lane, then in the canonical log and highway.

    Args:
        arc: The process ARC (clio-core segment store).
        event: The task layer's ``SemanticEvent``.

    Returns:
        What :meth:`ARCMemory.record_semantic_event` returns (the highway projection).
    """
    session_id = str(getattr(event, "session_id", "") or "")
    content = build_event_content(event)
    if session_id and content is not None and content["event_type"] in TASK_EVENTS:
        store = arc._segments
        scope = lane_chunking.chunk_for_append(
            store, session_id, TASK_EVENT_LANE, capacity=_capacity(arc)
        )
        store.append(
            session_id,
            scope,
            "semantic_event",
            content,
            step=-1,
            turn_id=str(getattr(event, "turn_id", "") or ""),
            expert_span_id=str(getattr(event, "expert_span_id", "") or ""),
        )
    return arc.record_semantic_event(event)


def session_task_events(arc: ARCMemory, session_id: str) -> Iterator[tuple[str, dict[str, Any]]]:
    """Yield one session's task events in append order, from the lane only."""
    for segment in lane_chunking.lane_segments(arc._segments, session_id, TASK_EVENT_LANE):
        content = segment.content
        if isinstance(content, dict) and str(content.get("event_type", "")) in TASK_EVENTS:
            yield session_id, content


def migration_owed(history: TaskHistory) -> bool:
    """Whether this store still owes the one-time copy (no current done marker)."""
    marker = history.index.done_marker(LANE_DONE)
    return marker is None or marker.get("version") != LANE_VERSION


def _forget(store: Any, session_id: str, scopes: list[str]) -> None:
    """Drop in-memory copies read or written by the copy; clio-core keeps the records."""
    for scope in scopes:
        with store._lock_for(session_id, scope):
            store._discard_scope(session_id, scope)


def _copy_session(arc: ARCMemory, session_id: str) -> int:
    """Rebuild one session's lane from its ``_events`` log; return the events copied.

    Any lane already present (a copy interrupted before the marker) is dropped first,
    so the result is exactly the log's task events whatever ran before.
    """
    store = arc._segments
    source = lane_chunking.lane_scopes(store, session_id, EVENTS_SCOPE)
    rows: list[Segment] = [
        segment
        for scope in source
        for segment in store.render(session_id, scope)
        if segment.kind == "semantic_event"
        and isinstance(segment.content, dict)
        and str(segment.content.get("event_type", "")) in TASK_EVENTS
    ]
    rows.sort(key=lambda segment: segment.logical_time)
    lane_chunking.drop_lane(store, session_id, TASK_EVENT_LANE)
    capacity = _capacity(arc)
    written: list[str] = []
    for index, start in enumerate(range(0, len(rows), capacity), start=1):
        scope = lane_chunking.chunk_scope(TASK_EVENT_LANE, index)
        chunk = [
            msgspec.structs.replace(segment, scope=scope, order=float(position + 1))
            for position, segment in enumerate(rows[start : start + capacity])
        ]
        # One put per chunk, as recorded: identities, clock and trace_ref stay those of
        # the original ``_events`` write, so this copy is not a new context op.
        with store._lock_for(session_id, scope):
            store._discard_scope(session_id, scope)
            store._put_scope(session_id, scope, chunk)
        written.append(scope)
    probes = [lane_chunking.chunk_scope(EVENTS_SCOPE, len(source) + 1)]
    probes.append(lane_chunking.chunk_scope(TASK_EVENT_LANE, len(written) + 1))
    _forget(store, session_id, [*source, *written, *probes])
    return len(rows)


def migrate_task_event_lane(history: TaskHistory) -> dict[str, Any]:
    """Copy every session's task events into the lane once, then write the marker.

    The install/upgrade step's entry point. Idempotent: a rerun (marker absent after
    an interruption) rebuilds each lane from the ``_events`` log. It holds the task
    history's writer lock, so no task event is written while it runs, and it keeps
    one session's log in memory at a time.

    Args:
        history: The task history over the store to migrate.

    Returns:
        The marker it wrote: version, completion time, sessions scanned, sessions
        with task events, events copied and seconds taken.
    """
    started = time.monotonic()
    arc = history.arc
    with history.serialized():
        sessions = arc._segments.sessions_with_scope(EVENTS_SCOPE)
        copied = with_events = 0
        for session_id in sessions:
            count = _copy_session(arc, session_id)
            copied += count
            with_events += count > 0
        report: dict[str, Any] = {
            "version": LANE_VERSION,
            "completed_at": datetime.now(UTC).isoformat(),
            "sessions": len(sessions),
            "sessions_with_task_events": with_events,
            "events": copied,
            "seconds": round(time.monotonic() - started, 3),
        }
        history.index.put_done_marker(LANE_DONE, report)
    return report


def ensure_task_event_lane(history: TaskHistory) -> dict[str, Any] | None:
    """Boot check: run the one-time copy when the store still owes it.

    Interim until the install/upgrade step runs :func:`migrate_task_event_lane`; then
    boot should refuse with ``reason=task_event_lane_migration_owed`` instead.

    Returns:
        The migration report when it ran, ``None`` when the marker was present.
    """
    if not migration_owed(history):
        logger.info("task_event_lane migration=skipped reason=marker_present")
        return None
    logger.warning(
        "task_event_lane migration=running reason=marker_absent at=boot "
        "(owed by the install/upgrade step)"
    )
    try:
        report = migrate_task_event_lane(history)
    except Exception as error:
        logger.error(
            "task_event_lane migration=failed reason=task_event_lane_migration_failed "
            "error=%r (no marker written; the next start reruns it)",
            error,
        )
        raise
    logger.warning(
        "task_event_lane migration=done at=boot sessions=%d with_task_events=%d events=%d "
        "seconds=%.2f",
        report["sessions"],
        report["sessions_with_task_events"],
        report["events"],
        report["seconds"],
    )
    return report


__all__ = [
    "LANE_DONE",
    "TASK_EVENTS",
    "TASK_EVENT_LANE",
    "ensure_task_event_lane",
    "migrate_task_event_lane",
    "migration_owed",
    "record",
    "session_task_events",
]
