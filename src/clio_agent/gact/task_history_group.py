"""Group commit of the task-history writer: several owner operations, one flush.

The history writer (``TaskHistory.io``, one thread) used to confirm every record of
an operation with its own round trip. A read-only MCP call's record (submission,
inline result, settled snapshot) was about 13 serialized clio-core RPCs, and in a P7
burst of 2,000 ``get_record`` calls the writer thread spent 98% of its active time
waiting on them, one at a time (py-spy, 2026-11-03).

:func:`run_grouped` runs several operations in one writer turn and confirms them
together. Each operation runs exactly as it would alone, against the same validation
and the same reads, except that its writes are staged:

* **Segment appends** (task content, the task-event lane, the ``_events`` log) are
  staged per scope and put once per scope (:mod:`clio_agent.arc.segment_group`), in
  ranks: task content and other cited records first, then the lane, then ``_events``
  (:func:`flush_rank`), the order a single operation writes them in.
* **Records it creates** (content-addressed task content) are probed for in one
  pipelined round trip for the whole group, not one cold read each
  (:func:`~clio_agent.arc.segment_group.cache_absent`).
* **Task index rows** (``task_lookup`` / ``task_open`` puts) are staged and put before
  any segment, one ``put_many`` per kind, so an opening record still precedes its
  event (:mod:`clio_agent.gact.task_index_records` superset invariant).
* **What follows a confirmed write** -- the tree projection and its listeners, the
  data-highway emission of each event, the UI hint, a closing ``task_open`` delete --
  runs after the flush, operation by operation, in the operation's own context
  (:func:`after_commit`). No reader on another thread sees a projection, highway event
  or index deletion before clio-core holds the records.

Identities the caller minted for an operation (a fresh handle and submission
reference) are read as absent instead of asked of clio-core
(:meth:`TaskIndex.assume_absent`): nothing can have recorded them yet.

Failure keeps today's per-operation outcome. An operation that raises while staging
fails alone (its staged writes are still confirmed, as its earlier separate writes
were). A failed read that holds recording (``requires_recovery``) stops the group: no
staged write is put and every operation fails. A failed flush sets
``requires_recovery`` and every operation fails; the caller hands each to the existing
recovery path, whose retries run them one at a time with ordinary reads.
"""

from __future__ import annotations

import contextvars
import logging
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from clio_agent.arc.lane_chunking import chunk_index, is_chunk_of
from clio_agent.arc.live import EVENTS_SCOPE
from clio_agent.arc.segment_group import cache_absent, segment_group

if TYPE_CHECKING:
    from clio_agent.arc.task_types import TaskSnapshot
    from clio_agent.gact.task_history import TaskHistory

__all__ = ["GroupedOperation", "after_commit", "flush_rank", "in_group", "run_grouped"]

logger = logging.getLogger(__name__)

_LANE = f"{EVENTS_SCOPE}/t/log"  # task_event_lane.TASK_EVENT_LANE (not imported: cycle)
_ACTIVE = threading.local()


@dataclass
class GroupedOperation:
    """One owner operation for :func:`run_grouped`.

    Attributes:
        write: The operation (as it would run alone on the history writer).
        minted: Lookup names nothing can have recorded yet
            (:func:`~clio_agent.gact.task_index_records.minted_names`).
        context: The context to run it and its post-commit work in.
        creates: The ``(session, scope)`` records it may create (its task content):
            probed together for the whole group (:func:`segment_group.cache_absent`).
        minted_scopes: Records it creates under a name derived from an identity just
            minted (its submission content): cached empty without asking.
    """

    write: Callable[[], Any]
    minted: tuple[str, ...] = ()
    context: contextvars.Context | None = None
    creates: Callable[[], Iterable[tuple[str, str]]] | None = None
    minted_scopes: tuple[tuple[str, str], ...] = ()


@dataclass
class _Item:
    operation: GroupedOperation
    after: list[Callable[[], Any]] = field(default_factory=list)
    ok: bool = False
    value: Any = None

    def run(self, call: Callable[[], Any]) -> Any:
        context = self.operation.context
        return context.run(call) if context is not None else call()


@dataclass
class _Group:
    history: TaskHistory
    current: _Item | None = None


def _active(history: TaskHistory) -> _Group | None:
    group: _Group | None = getattr(_ACTIVE, "group", None)
    return group if group is not None and group.history is history else None


def in_group(history: TaskHistory) -> bool:
    """Whether this thread is staging an operation of a group over ``history``."""
    group = _active(history)
    return group is not None and group.current is not None


def after_commit(history: TaskHistory, work: Callable[[], Any]) -> None:
    """Run ``work`` now, or once the active group's records are confirmed."""
    group = _active(history)
    if group is None or group.current is None:
        work()
        return
    group.current.after.append(work)


def flush_rank(scope: str) -> tuple[int, int]:
    """Records an event may cite first, then the task-event lane, then ``_events``."""
    if is_chunk_of(_LANE, scope):
        return (1, chunk_index(_LANE, scope))
    if is_chunk_of(EVENTS_SCOPE, scope):
        return (2, chunk_index(EVENTS_SCOPE, scope))
    return (0, 0)


def observation_hint(history: TaskHistory, snapshot: TaskSnapshot) -> None:
    """The UI hint after a confirmed observation; its failure never undoes the record."""
    if history.observation_committed is None:
        return
    try:
        history.observation_committed(snapshot)
    except Exception:
        logger.exception(
            "Task UI hint failed after confirmed observation handle=%s", snapshot.handle
        )


def run_grouped(history: TaskHistory, operations: list[GroupedOperation]) -> list[tuple[bool, Any]]:
    """Run ``operations`` on this (writer) thread and confirm their records together.

    Returns:
        Per operation, ``(True, its return value)`` or ``(False, the exception)``.
    """
    from clio_agent.gact.task_history import RecordingUnconfirmed  # noqa: PLC0415 - cycle

    items = [_Item(operation) for operation in operations]
    group = _Group(history)
    try:
        with history._writing():
            with (
                segment_group(history.arc._segments, flush_rank) as segments,
                history.index.staged() as rows,
            ):
                operations = [item.operation for item in items]
                cache_absent(
                    history.arc._segments,
                    [key for op in operations if op.creates for key in op.creates()],
                    minted=[key for op in operations for key in op.minted_scopes],
                )
                _ACTIVE.group = group
                try:
                    for item in items:
                        if history.requires_recovery:
                            break
                        group.current = item
                        try:
                            with history.index.assume_absent(item.operation.minted):
                                item.value, item.ok = item.run(item.operation.write), True
                        except Exception as error:  # noqa: BLE001 - forwards: its caller
                            item.value = error
                        finally:
                            group.current = None
                finally:
                    _ACTIVE.group = None
                if history.requires_recovery:
                    raise RecordingUnconfirmed("Core observations require reconciliation")
                try:
                    history.index.put_staged(rows)
                    segments.flush()
                except Exception:
                    history.requires_recovery = True
                    raise
            for item in items:
                for work in item.after:
                    try:
                        item.run(work)
                    except Exception as error:  # noqa: BLE001 - forwards: its caller
                        if item.ok:
                            item.ok, item.value = False, error
    except Exception as error:  # noqa: BLE001 - forwards: every operation's caller
        for item in items:
            if item.ok or item.value is None:
                item.ok, item.value = False, error
    return [(item.ok, item.value) for item in items]
