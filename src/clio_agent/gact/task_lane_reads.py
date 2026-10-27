"""What a task tree needs from content, recorded in the task-event lane when it is known.

Replaying a tree used to read clio-core content for every observation: the assignment
``details`` of each Session/Subagent snapshot (to find its workflow checkpoint), the
checkpoint itself, each task's description, and each prepared model input's whole
context scope (to learn whether the input was committed). Each read is one RPC, and
together they were most of a tree load. The writer already holds these facts, so they
go into the lane with the event:

* **Observation annotations** (``payload["lane"]`` of a lane ``task.observed``):
  ``description`` (the text of a newly referenced description) and ``workflow``
  (the workflow reference in the snapshot's details, ``None`` when there is none,
  and the checkpoint identity when the reference is not already the root's latest
  canonical checkpoint). Written only when the description reference or the details
  change, as replay reads them only then.
* **Input receipts** (lane-only ``session.input.committed`` events): the exact
  :class:`RecipientInput` once its context append succeeded. The context scope stays
  the commit point; a receipt is written after it, so a missing receipt (a crash in
  between, or an older store) means replay reads the scope as before.

Replay uses an annotation or receipt when present and reads content only when one is
absent. Stores written before this get both from :func:`migrate_task_lane_reads`, a
one-time pass with its own ``meta.task_lane_reads`` done marker, run after the lane
copy (:func:`clio_agent.gact.task_event_lane.migrate_task_event_lane`, which clears
this marker whenever it rebuilds lanes).
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import msgspec

from clio_agent.arc import lane_chunking
from clio_agent.arc.schema import Segment
from clio_agent.arc.task_types import (
    AssignmentDetails,
    CoreReference,
    RecipientInput,
    TaskSnapshot,
    argument_digest,
)
from clio_agent.errors import ClioError
from clio_agent.gact import task_event_lane
from clio_agent.gact.task_event_lane import LANE_KEY, READS_DONE, TASK_EVENT_LANE

if TYPE_CHECKING:
    from collections.abc import Callable

    from clio_agent.gact.task_history import TaskHistory
    from clio_agent.gact.task_tree_state import TreeState

logger = logging.getLogger(__name__)

INPUT_COMMITTED = "session.input.committed"
READS_VERSION = 1
_WORKFLOW_KINDS = frozenset({"Session", "Subagent"})
_READ_ERRORS = (ValueError, OSError, ClioError)


# --------------------------------------------------------------------------- #
# Workflow provenance from assignment details
# --------------------------------------------------------------------------- #


def checkpoint_identity(content: Any) -> dict[str, Any]:
    """The fields of a historical checkpoint body that replay validates and projects."""
    if not isinstance(content, dict):
        return {"complete": False}
    complete = all(
        isinstance(content.get(key), str) and content[key]
        for key in ("id", "invocation_id", "declaration")
    ) and isinstance(content.get("request"), str)
    if not complete:
        return {"complete": False}
    identity = {
        "complete": True,
        "id": content["id"],
        "invocation_id": content["invocation_id"],
        "declaration": content["declaration"],
        "request_digest": argument_digest({"request": content["request"]}),
    }
    # Owner fields only when the body names them (an absent one means "this owner").
    identity.update(
        {key: content[key] for key in ("root_handle", "root_session") if key in content}
    )
    return identity


def workflow_annotation(
    read: Callable[[CoreReference], Any],
    snapshot: TaskSnapshot,
    *,
    known: CoreReference | None = None,
) -> dict[str, Any]:
    """Read a snapshot's details once and record its workflow reference.

    Args:
        read: The history's validated content reader.
        snapshot: A Session/Subagent snapshot with ``details``.
        known: The root's latest canonical checkpoint content, if any: a reference
            equal to it needs no checkpoint identity.

    Returns:
        ``{"reference": None}`` without a workflow, else the reference and, unless it
        is ``known``, the checkpoint identity.

    Raises:
        ValueError, OSError, ClioError: The details or checkpoint are unreadable.
    """
    if snapshot.details is None:
        raise ValueError("Snapshot has no assignment details")
    raw = read(snapshot.details)
    if not isinstance(raw, dict):
        raise ValueError("Historical assignment details are not an object")
    workflow = AssignmentDetails.model_validate(raw).workflow
    if workflow is None:
        return {"reference": None}
    reference = CoreReference.model_validate(workflow)
    annotation: dict[str, Any] = {"reference": reference.model_dump(mode="json")}
    if reference != known:
        annotation["checkpoint"] = checkpoint_identity(read(reference))
    return annotation


def _needs_workflow(previous: TaskSnapshot | None, snapshot: TaskSnapshot) -> bool:
    """Replay decodes details exactly when they are new for this handle."""
    return (
        snapshot.kind in _WORKFLOW_KINDS
        and snapshot.details is not None
        and (previous is None or previous.details != snapshot.details)
    )


def observed_lane(
    history: TaskHistory,
    state: TreeState,
    previous: TaskSnapshot | None,
    snapshot: TaskSnapshot,
) -> dict[str, Any] | None:
    """The lane annotations of one observation being written (``None`` when none apply).

    The description text is in hand. Details and a checkpoint are read once here,
    normally from the segment cache (the owner wrote them just before). An unreadable
    details record is left unannotated with a typed log line; replay then reads it,
    exactly as before annotations existed.
    """
    lane: dict[str, Any] = {}
    reference = snapshot.description_reference
    if (
        reference is not None
        and isinstance(snapshot.description, str)
        and (previous is None or previous.description_reference != reference)
    ):
        lane["description"] = snapshot.description
    if _needs_workflow(previous, snapshot):
        latest = state._workflow_latest.get(snapshot.handle)
        try:
            lane["workflow"] = workflow_annotation(
                history.read_content,
                snapshot,
                known=latest.content if latest is not None else None,
            )
        except _READ_ERRORS as error:
            logger.warning(
                "task_lane_annotation skipped reason=details_unreadable handle=%s error=%s",
                snapshot.handle,
                type(error).__name__,
            )
    return lane or None


# --------------------------------------------------------------------------- #
# Committed model inputs
# --------------------------------------------------------------------------- #


def receipt_content(record: RecipientInput) -> dict[str, Any]:
    """The lane record proving an input reached its context scope."""
    return {
        "event_type": INPUT_COMMITTED,
        "status": "",
        "summary": "",
        "actor": {},
        "subject": {},
        "payload": {
            "schema_version": 1,
            "observation_id": record.id,
            "record": record.model_dump(mode="json"),
        },
        "provider": {},
        "occurred_at": record.committed_at,
        "trace_id": "",
    }


def accept_committed(history: TaskHistory, state: TreeState, record: RecipientInput) -> None:
    """After the context append: write the input's receipt, then accept it in memory."""
    from clio_agent.gact.task_history_projection import _accept_input

    task_event_lane.append_lane(history.arc, record.session_id, receipt_content(record))
    _accept_input(state, record)


def receipt_record(payload: dict[str, Any], locator: dict[str, Any]) -> RecipientInput:
    """Validate a lane receipt against the prepared input it answers."""
    record = RecipientInput.model_validate(payload["record"])
    if (
        record.session_id != locator["session_id"]
        or record.id != locator["id"]
        or record.boundary.scope != locator["scope"]
    ):
        raise ValueError("Input receipt belongs to a different context")
    return record


# --------------------------------------------------------------------------- #
# One-time pass for stores written before annotations and receipts
# --------------------------------------------------------------------------- #


def reads_owed(history: TaskHistory) -> bool:
    """Whether this store still owes the one-time pass (no current done marker)."""
    marker = history.index.done_marker(READS_DONE)
    return marker is None or marker.get("version") != READS_VERSION


def _annotate_rows(
    history: TaskHistory, rows: list[Segment], stats: dict[str, int]
) -> list[Segment]:
    """Annotate one session's lane rows in replay order; return the rows, changed or not."""
    previous: dict[str, TaskSnapshot] = {}
    latest: dict[str, CoreReference] = {}
    out: list[Segment] = []
    for segment in rows:
        content = segment.content
        payload = content.get("payload") or {}
        event_type = content.get("event_type")
        if event_type == "workflow.checkpoint" and "record" in payload:
            record = payload["record"]
            latest[str(record.get("root_handle"))] = CoreReference.model_validate(record["content"])
        if event_type != "task.observed" or "record" not in payload:
            out.append(segment)
            continue
        try:
            snapshot = TaskSnapshot.read_history(payload["record"])
        except ValueError:
            stats["unreadable"] += 1  # replay meets the same record and reports it
            out.append(segment)
            continue
        prior = previous.get(snapshot.handle)
        previous[snapshot.handle] = snapshot
        if LANE_KEY in payload:
            out.append(segment)
            continue
        lane: dict[str, Any] = {}
        reference = snapshot.description_reference
        if reference is not None and (prior is None or prior.description_reference != reference):
            try:
                text = history.read_content(reference)
            except _READ_ERRORS:
                text = None
            if isinstance(text, str):
                lane["description"] = text
            else:
                stats["unreadable"] += 1
        if _needs_workflow(prior, snapshot):
            try:
                lane["workflow"] = workflow_annotation(
                    history.read_content, snapshot, known=latest.get(snapshot.handle)
                )
            except _READ_ERRORS:
                stats["unreadable"] += 1
        if not lane:
            out.append(segment)
            continue
        stats["annotated"] += 1
        content = {**content, "payload": {**payload, LANE_KEY: lane}}
        out.append(msgspec.structs.replace(segment, content=content))
    return out


def _receipts(
    history: TaskHistory, session_id: str, rows: list[Segment], stats: dict[str, int]
) -> list[dict[str, Any]]:
    """Receipts for prepared inputs already committed to context but not yet receipted."""
    receipted: set[str] = set()
    locators: dict[str, dict[str, Any]] = {}
    for segment in rows:
        event_type = segment.content.get("event_type")
        payload = segment.content.get("payload") or {}
        if event_type == INPUT_COMMITTED:
            receipted.add(str(payload["record"]["id"]))
        elif event_type == "session.input.prepared" and "record" in payload:
            locator = payload["record"]
            if locator.get("session_id") == session_id:
                locators[str(locator["id"])] = locator
    scopes: dict[str, dict[str, RecipientInput]] = {}
    out: list[dict[str, Any]] = []
    for identity, locator in locators.items():
        if identity in receipted:
            continue
        scope = str(locator["scope"])
        if scope not in scopes:
            try:
                scopes[scope] = history.context_inputs(session_id, scope)
            except _READ_ERRORS:
                stats["unreadable"] += 1  # replay reads it, and reports it, as before
                scopes[scope] = {}
        record = scopes[scope].get(identity)
        if record is not None:
            out.append(receipt_content(record))
    return out


def _rewrite_lane(
    history: TaskHistory,
    session_id: str,
    chunks: list[tuple[str, list[Segment]]],
    changed: dict[str, Segment],
    receipts: list[dict[str, Any]],
) -> None:
    """Rewrite only the chunks that changed, each in place, then append receipts at the tail.

    Every row stays in the chunk it was in (one put replaces a chunk atomically), and
    receipts only extend the last chunk or open new ones, so a pass stopped midway
    leaves each chunk old or annotated, never a hole or a moved row; a rerun skips
    the annotated rows and the receipted inputs and finishes the rest.
    """
    store = history.arc._segments
    capacity = int(getattr(history.arc, "_events_chunk_segments", 512))
    tail: list[Segment] = []
    for content in receipts:
        tail.append(
            Segment(
                scope=TASK_EVENT_LANE,
                kind="semantic_event",
                content=content,
                session_id=session_id,
                step=-1,
                order=0.0,
                logical_time=store._new_lt(),
            )
        )
    rewritten = {scope: [changed.get(row.id, row) for row in rows] for scope, rows in chunks}
    dirty = {scope for scope, rows in chunks if any(row.id in changed for row in rows)}
    last = chunks[-1][0]
    room = max(capacity - len(rewritten[last]), 0)
    if tail and room:
        rewritten[last] = rewritten[last] + tail[:room]
        dirty.add(last)
        tail = tail[room:]
    for index, start in enumerate(range(0, len(tail), capacity), start=len(chunks) + 1):
        scope = lane_chunking.chunk_scope(TASK_EVENT_LANE, index)
        rewritten[scope] = tail[start : start + capacity]
        dirty.add(scope)
    for scope, rows in rewritten.items():  # ascending chunk order
        if scope not in dirty:
            continue
        chunk = [
            msgspec.structs.replace(segment, scope=scope, order=float(position + 1))
            for position, segment in enumerate(rows)
        ]
        with store._lock_for(session_id, scope):
            store._discard_scope(session_id, scope)
            store._put_scope(session_id, scope, chunk)
    lane_chunking.forget_cursor(store, session_id, TASK_EVENT_LANE)


def _pass_session(history: TaskHistory, session_id: str, stats: dict[str, int]) -> None:
    store = history.arc._segments
    chunks = [
        (scope, store.list_segments(session_id, scope, include_tombstoned=True))
        for scope in lane_chunking.lane_scopes(store, session_id, TASK_EVENT_LANE)
    ]
    if not chunks:
        return
    rows = sorted((row for _scope, part in chunks for row in part), key=lambda r: r.logical_time)
    annotated = _annotate_rows(history, rows, stats)
    changed = {new.id: new for old, new in zip(rows, annotated, strict=True) if new is not old}
    receipts = _receipts(history, session_id, annotated, stats)
    stats["receipts"] += len(receipts)
    if changed or receipts:
        _rewrite_lane(history, session_id, chunks, changed, receipts)
        stats["sessions_rewritten"] += 1
    # The pass read this session's lane, content and context scopes: drop the copies.
    store.release(session_id)


def migrate_task_lane_reads(history: TaskHistory) -> dict[str, Any]:
    """Annotate every lane and receipt every committed input once, then write the marker.

    The install/upgrade step's entry point, run after
    :func:`~clio_agent.gact.task_event_lane.migrate_task_event_lane`. Idempotent: an
    annotated row or a receipted input is left as it is, so a rerun after an
    interruption finishes the rest. Holds the history's writer lock and one session's
    rows at a time.

    Args:
        history: The task history over the store to migrate.

    Returns:
        The marker it wrote: version, completion time, sessions, sessions rewritten,
        observations annotated, receipts written, unreadable details, seconds.

    Raises:
        RuntimeError: The lane copy has not run yet (``task_event_lane_migration_owed``).
    """
    if task_event_lane.migration_owed(history):
        raise RuntimeError("reason=task_event_lane_migration_owed: copy the lane first")
    started = time.monotonic()
    stats = {"annotated": 0, "receipts": 0, "unreadable": 0, "sessions_rewritten": 0}
    with history.serialized():
        sessions = history.arc._segments.sessions_with_scope(TASK_EVENT_LANE)
        for session_id in sessions:
            _pass_session(history, session_id, stats)
        report: dict[str, Any] = {
            "version": READS_VERSION,
            "completed_at": datetime.now(UTC).isoformat(),
            "sessions": len(sessions),
            **stats,
            "seconds": round(time.monotonic() - started, 3),
        }
        history.index.put_done_marker(READS_DONE, report)
    return report


__all__ = [
    "INPUT_COMMITTED",
    "accept_committed",
    "checkpoint_identity",
    "migrate_task_lane_reads",
    "observed_lane",
    "reads_owed",
    "receipt_content",
    "receipt_record",
    "workflow_annotation",
]
