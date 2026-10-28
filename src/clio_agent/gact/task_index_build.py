"""Task history start/reset and the one-time build of the derived records.

After the build marker exists, starting (or recovering from a write failure)
reads nothing: it drops cached trees and the next use of a tree reloads it from
its own sessions. Only a store without the marker is replayed in full, once:
the replay feeds the historical migrations, then every lookup/open record is
derived from it, subscriptions are re-filed under their task owners, and the
marker is written last. A crash before the marker reruns the build; every step
is idempotent.

clio-core has no compare-and-swap, so on a multi-node deployment the build must
run once, before other nodes start (an offline step or a single-builder lease).
"""

from __future__ import annotations

import hashlib
import json
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from clio_agent.arc import read_ahead
from clio_agent.arc.live import EVENTS_SCOPE
from clio_agent.arc.loop_guard import assert_store_write_off_loop
from clio_agent.gact import task_history_queries
from clio_agent.gact.task_index_records import (
    LOOKUP_KIND,
    OPEN_KIND,
    Row,
    display_open_row,
    handle_row,
    list_row,
    mail_open_row,
    members_row,
    reference_row,
    session_row,
    task_open_row,
    workflow_open_row,
)
from clio_agent.gact.task_index_writes import displayed, needs_record
from clio_agent.gact.task_tree_state import TreeState, replay

if TYPE_CHECKING:
    from clio_agent.gact.task_history import TaskHistory


def restore(history: TaskHistory) -> None:
    """Reset to Core: drop cached trees, or replay once before the records exist."""
    assert_store_write_off_loop("task.restore")
    started = time.monotonic()
    with history._lock:
        try:
            built = history.index.marker() is not None
            candidate = None if built else _full_replay(history)
        except Exception:
            history.requires_recovery = True
            raise
        with history._view_lock:
            history._global = candidate
            history.index_build_pending = candidate is not None
            history.trees.clear()
            history.index.clear_caches()
            history.revision += 1
            history.replay_seconds = time.monotonic() - started
            history.requires_recovery = False
    task_history_queries.notify(history, restored=True)


#: The full replay read every session's task-event lane (annotated, receipted).
REPLAY_FROM_LANES = "task_event_lanes"
#: The full replay read every session's whole ``_events`` log: the lanes were not
#: (yet) complete, as at a boot whose lane migration is still owed.
REPLAY_FROM_EVENTS_LOG = "events_log_lanes_owed"
_EVENTS_LOG_RE = r".*__events(~[1-9][0-9]*)?"
_LANE_RE = r".*__events~t~log(~[1-9][0-9]*)?"


def _full_replay(history: TaskHistory) -> TreeState:
    """The pre-build projection over every session (the build's only global read).

    It reads each session's task-event lane when the lane migrations are done: the
    lane holds exactly the log's task events, with the content replay would otherwise
    read (descriptions, workflow checkpoints, committed inputs) recorded beside them.
    Before that (a boot whose lane migration is still owed) it reads the whole
    ``_events`` logs. Either way the logs are read ahead a batch of sessions at a time
    (:mod:`clio_agent.arc.read_ahead`); ``history.replay_source`` records which.
    """
    from clio_agent.gact import task_event_lane, task_lane_reads  # noqa: PLC0415

    arc = history.arc
    store = arc._segments
    candidate = TreeState(history, "", ())
    lanes = not task_event_lane.migration_owed(history) and not task_lane_reads.reads_owed(history)
    history.replay_source = REPLAY_FROM_LANES if lanes else REPLAY_FROM_EVENTS_LOG
    base = task_event_lane.TASK_EVENT_LANE if lanes else EVENTS_SCOPE
    with read_ahead.reading_ahead(store._store) as buffer:
        sessions = store.sessions_with_scope(base)
        names = (
            read_ahead.listed(store, _LANE_RE if lanes else _EVENTS_LOG_RE)
            if buffer is not None
            else set()
        )
        ahead = read_ahead.ahead_by_batch(
            store._store,
            "segments",
            sessions,
            lambda session_id: read_ahead.family_names(store, session_id, base, names),
        )
        if lanes:
            events = (
                event
                for session_id in ahead
                for event in task_event_lane.session_task_events(arc, session_id)
            )
        else:
            events = (
                (session_id, segment.content)
                for session_id in ahead
                for segment in arc._live.iter_session_event_segments(session_id)
            )
        replay(candidate, events, owner_filed_subscriptions=False)
    return candidate


def complete_index_build(history: TaskHistory) -> None:
    """Derive every record from the build's replay, write the marker, go lazy."""
    assert_store_write_off_loop("task.index.build")
    with history._writing():
        state = history._global
        if state is None:
            return
        _refile_subscriptions(history, state)
        lookup, opened = derive_records(state)
        history.confirm_write(lambda: history.index.put_many(LOOKUP_KIND, lookup))
        history.confirm_write(lambda: history.index.put_many(OPEN_KIND, opened))
        built_at = datetime.now(UTC).isoformat()
        history.confirm_write(lambda: history.index.put_marker(built_at))
        with history._view_lock:
            history._global = None
            history.index_build_pending = False
            history.trees.clear()
            history.index.clear_caches()


def _refile_subscriptions(history: TaskHistory, state: TreeState) -> None:
    """Copy subscriptions filed under a subscriber into the task owner's log."""
    for handle, rows in state._subscriptions.items():
        task = state._tasks.get(handle)
        if task is None:
            continue
        owner = task.owner.session_id
        for recipient, subscription in rows.items():
            if state.subscription_files.get((handle, recipient)) == owner:
                continue
            payload = subscription.model_dump(mode="json")
            digest = hashlib.sha256(
                json.dumps([handle, recipient, payload], sort_keys=True).encode()
            ).hexdigest()
            history._record("task.subscription", owner, "migration:subscription:" + digest, payload)
            state.subscription_files[(handle, recipient)] = owner


def tree_roots(state: TreeState) -> dict[str, str]:
    """Session -> tree root for every session holding a task, by lineage components."""
    parent: dict[str, str] = {}

    def find(session: str) -> str:
        parent.setdefault(session, session)
        while parent[session] != session:
            parent[session] = parent[parent[session]]
            session = parent[session]
        return session

    for task in state._tasks.values():
        session = find(task.owner.session_id)
        lineage = state._tasks.get(task.owner.parent_handle or "")
        if lineage is not None:
            other = find(lineage.owner.session_id)
            if other != session:
                parent[other] = session
    components: dict[str, list[str]] = {}
    for session in list(parent):
        components.setdefault(find(session), []).append(session)
    origins = sorted(
        (task.created_at, task.owner.session_id)
        for task in state._tasks.values()
        if task.owner.parent_handle not in state._tasks
    )
    roots: dict[str, str] = {}
    for members in components.values():
        member_set = set(members)
        root = next((sid for _at, sid in origins if sid in member_set), min(members))
        for member in members:
            roots[member] = root
    return roots


def derive_records(state: TreeState) -> tuple[list[Row], list[Row]]:
    """Every lookup and open record implied by one full projection."""
    roots = tree_roots(state)
    lookup: list[Row] = []
    opened: list[Row] = []
    members: dict[str, set[str]] = {}
    humans: dict[str, set[str]] = {}
    session_humans: dict[str, str] = {}
    for handle, task in state._tasks.items():
        owner = task.owner
        lookup.append(
            handle_row(handle, owner.session_id, task.kind, owner.human_id, task.created_at)
        )
        root = roots[owner.session_id]
        members.setdefault(root, set()).add(owner.session_id)
        humans.setdefault(owner.human_id, set()).add(root)
        session_humans.setdefault(owner.session_id, owner.human_id)
        if needs_record(task):
            opened.append(
                task_open_row(handle, owner.session_id, task.kind, owner.human_id, task.created_at)
            )
    for session_id, root in roots.items():
        lookup.append(session_row(session_id, root, session_humans.get(session_id, "")))
    lookup.extend(
        members_row(root, sorted(rows)) for root, rows in members.items() if len(rows) > 1
    )
    lookup.extend(list_row("hum.", human, sorted(rows)) for human, rows in humans.items())
    for reference, content in state._submissions.items():
        lookup.append(reference_row(reference, "", content.session_id))
    for alias, rows in state._aliases.items():
        lookup.append(list_row("a.", alias, [row.model_dump(mode="json") for row in rows.values()]))
    for resource, handles in state._resource_tasks.items():
        lookup.append(list_row("res.", resource, sorted(handles)))
    for session_id, pending in state._pending.items():
        opened.extend(mail_open_row(session_id, mail_id) for mail_id in pending)
    opened.extend(workflow_open_row(root) for root in state._workflow_dirty)
    for session_id, identities in state._acknowledged.items():
        for mail_id in identities:
            item = state.mail_item(session_id, mail_id)
            if item is not None and displayed(item):
                opened.append(display_open_row(session_id, mail_id))
    return lookup, opened
