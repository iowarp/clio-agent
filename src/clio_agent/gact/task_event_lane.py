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
  store migration ``task_event_lane`` (:mod:`clio_agent.gact.store_migrations`):
  ``clio-agent migrate`` runs it at install or upgrade; a boot that finds it owed runs
  it once with the typed reason ``task_event_lane_migration_owed``.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from functools import partial
from typing import TYPE_CHECKING, Any

import msgspec

from clio_agent.arc import lane_chunking
from clio_agent.arc.live import EVENTS_SCOPE, build_event_content
from clio_agent.arc.loop_guard import assert_store_write_off_loop
from clio_agent.gact import task_event_reconcile
from clio_agent.gact.task_history_group import after_commit, in_group
from clio_agent.gact.task_index_records import LOOKUP_KIND

if TYPE_CHECKING:
    from clio_agent.arc.memory import ARCMemory
    from clio_agent.arc.schema import Segment
    from clio_agent.gact.task_history import TaskHistory

#: Base scope of the lane's chunk family (chunk N >= 2 is ``_events/t/log/N``).
TASK_EVENT_LANE = f"{EVENTS_SCOPE}/t/log"
#: Done marker (``task_lookup`` record ``meta.task_event_lane``) of the one-time copy.
LANE_DONE = "task_event_lane"
#: Done marker of :func:`clio_agent.gact.task_lane_reads.migrate_task_lane_reads`.
READS_DONE = "task_lane_reads"
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
        # Lane-only: the receipt of a model input committed to its context scope
        # (:mod:`clio_agent.gact.task_lane_reads`); never in the ``_events`` log.
        "session.input.committed",
    }
)
#: Envelope key of the lane-only read annotations on a ``task.observed`` event.
LANE_KEY = "lane"


def _capacity(arc: ARCMemory) -> int:
    return int(getattr(arc, "_events_chunk_segments", 512))


def append_lane(
    arc: ARCMemory,
    session_id: str,
    content: dict[str, Any],
    *,
    turn_id: str = "",
    expert_span_id: str = "",
) -> None:
    """Append one record to a session's task-event lane (the lane's only append seam)."""
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
        turn_id=turn_id,
        expert_span_id=expert_span_id,
    )


def record(
    arc: ARCMemory,
    event: Any,
    *,
    lane: dict[str, Any] | None = None,
    highway: Callable[[Callable[[], Any]], None] | None = None,
    to_lane: bool = True,
    to_log: bool = True,
) -> Any:
    """Confirm a task event in its lane, then derive the highway from that record.

    The lane is the one record of a task event (:data:`TASK_EVENTS`): it is not also
    appended to the ``_events`` log, and readers of the whole log merge the lane in
    (:mod:`clio_agent.gact.task_event_merge`). Another event type (the human
    intervention, a migration receipt) goes to the ``_events`` log as before.

    Args:
        arc: The process ARC (clio-core segment store).
        event: The task layer's ``SemanticEvent``.
        lane: Read annotations stored with the lane copy only (under ``LANE_KEY`` in
            its payload), never in the canonical log.
        highway: Inside a writer group, where the highway emission goes (it runs once
            the group's records are confirmed, :mod:`clio_agent.gact.task_history_group`).
        to_lane: Append to the lane (a retry skips a lane that already holds the event,
            :mod:`clio_agent.gact.task_event_reconcile`).
        to_log: Emit the highway event, after appending a non-lane event to the
            ``_events`` log (likewise).

    Returns:
        What :meth:`ARCMemory.record_semantic_event` returns (the highway projection);
        ``None`` when the emission was handed to ``highway``.
    """
    session_id = str(getattr(event, "session_id", "") or "")
    content = build_event_content(event)
    in_lane = bool(session_id) and content is not None and content["event_type"] in TASK_EVENTS
    if to_lane and in_lane and content is not None:
        if lane:
            content["payload"] = {**content["payload"], LANE_KEY: lane}
        append_lane(
            arc,
            session_id,
            content,
            turn_id=str(getattr(event, "turn_id", "") or ""),
            expert_span_id=str(getattr(event, "expert_span_id", "") or ""),
        )
    if not to_log:
        return None
    if not in_lane:
        if highway is None:
            return arc.record_semantic_event(event)
        arc.on_semantic_event(event)  # the ``_events`` append half of record_semantic_event
    sink = arc._highway_sink
    if sink is None:
        return {}
    if highway is None:
        return sink(event)
    highway(lambda: sink(event))
    return None


def record_event(
    history: TaskHistory,
    event_type: str,
    session_id: str,
    identity: str,
    payload: dict[str, Any],
    *,
    lane: dict[str, Any] | None = None,
) -> None:
    """``TaskHistory._record``: one versioned task observation through the confirmed writer."""
    # Import only the existing data envelope; no GACT application or execution owner.
    from clio_agent.gact.semantic_events import SemanticEvent

    assert_store_write_off_loop("task.observation")
    event = SemanticEvent(
        event_type=event_type,
        session_id=session_id,
        trace_id="",
        span_id=identity,
        payload={"schema_version": 1, "observation_id": identity, "record": payload},
    )
    highway = partial(after_commit, history) if in_group(history) else None
    to_lane = to_log = True
    if task_event_reconcile.active(history):  # a retry: only where the first try did not land
        to_lane, to_log = task_event_reconcile.missing(history, event)
    history.confirm_write(
        lambda: record(
            history.arc, event, lane=lane, highway=highway, to_lane=to_lane, to_log=to_log
        )
    )


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

    Any lane already present (a copy interrupted before the marker) is rewritten with
    the log's task events merged in by identity: a task event written since the lane
    became its only record is kept, never dropped.
    """
    store = arc._segments
    source = lane_chunking.lane_scopes(store, session_id, EVENTS_SCOPE)
    present = lane_chunking.lane_scopes(store, session_id, TASK_EVENT_LANE)
    unique: dict[str, Segment] = {}
    for scope in [*source, *present]:
        for segment in store.render(session_id, scope):
            if (
                segment.kind == "semantic_event"
                and isinstance(segment.content, dict)
                and str(segment.content.get("event_type", "")) in TASK_EVENTS
            ):
                unique.setdefault(segment.id, segment)
    rows: list[Segment] = sorted(unique.values(), key=lambda segment: segment.logical_time)
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
        # A rebuilt lane holds no read annotations or input receipts: owe them again.
        history.index.store.delete(LOOKUP_KIND, "meta." + READS_DONE)
        history.index.put_done_marker(LANE_DONE, report)
    return report


__all__ = [
    "LANE_DONE",
    "LANE_KEY",
    "READS_DONE",
    "append_lane",
    "record_event",
    "TASK_EVENTS",
    "TASK_EVENT_LANE",
    "migrate_task_event_lane",
    "migration_owed",
    "record",
    "session_task_events",
]
