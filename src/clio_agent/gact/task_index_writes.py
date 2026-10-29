"""Ordered writes of the derived task records, reconciliation and tree eviction.

Ordering (superset invariant): an opening record is put before the event that
opens the obligation, inside the same writer section, and a failed put holds
admission like any other unconfirmed write. A closing record is deleted after
the event that closes it; a failed delete leaves a stale hint that is logged
with a typed reason and removed by :func:`reconcile_open` at the next boot.

During the one-time build (``history._global`` set) nothing here writes: the
build derives every record from its full replay instead.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from functools import partial
from typing import TYPE_CHECKING

from clio_agent.arc.task_types import (
    MailItem,
    RecipientInput,
    SubmissionContext,
    TaskAlias,
    TaskSnapshot,
)
from clio_agent.gact.task_index_records import (
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
from clio_agent.gact.task_tree_state import TreeState, load_tree

if TYPE_CHECKING:
    from clio_agent.gact.task_history import TaskHistory

logger = logging.getLogger(__name__)

MAX_CACHED_TREES = 256
_DISPLAYED = frozenset({"a2ui", "question_answer"})


def publication_owed(task: TaskSnapshot) -> bool:
    """A terminal outcome whose notice/publication boot recovery would still retry."""
    if (
        task.kind in {"Session", "Subagent"}
        and task.settlement in {"confirmed", "never_started"}
        and task.result is not None
    ):
        return True
    backend = task.backend
    return (
        backend is not None
        and backend.owner == "external_mcp"
        and task.backend_observation is not None
        and task.settlement == "external_acknowledged"
    )


def needs_record(task: TaskSnapshot | None) -> bool:
    """Whether a task belongs in ``task_open``: open, or terminal with publication owed."""
    if task is None:
        return False
    return task.terminal_at is None or publication_owed(task)


def displayed(item: MailItem) -> bool:
    """Committed mail whose owner display is repaired from the commit."""
    return item.kind in _DISPLAYED or (item.kind == "human" and item.id.startswith("a2ui:"))


def _close(history: TaskHistory, name: str) -> None:
    try:
        history.index.delete_open(name)
    except Exception as error:
        logger.exception(
            "Task boundary reason=task_open_close_deferred record=%s error_type=%s",
            name,
            type(error).__name__,
        )


# ---- tasks and trees ---------------------------------------------------------


def open_task(
    history: TaskHistory, previous: TaskSnapshot | None, snapshot: TaskSnapshot
) -> TreeState:
    """Write the records an observation needs, then return the tree to project into."""
    if history._global is not None:
        return history._global
    owner = snapshot.owner
    if previous is None:
        join_session(history, snapshot)
        history.confirm_write(
            lambda: history.index.put_lookup(
                handle_row(
                    snapshot.handle,
                    owner.session_id,
                    snapshot.kind,
                    owner.human_id,
                    snapshot.created_at,
                )
            )
        )
    resource = snapshot.resource_id
    if resource is not None and (previous is None or previous.resource_id != resource):
        handles = history.index.rows("res.", resource)
        if snapshot.handle not in handles:
            history.confirm_write(
                lambda: history.index.put_lookup(
                    list_row("res.", resource, sorted({*handles, snapshot.handle}))
                )
            )
    if needs_record(snapshot) and not needs_record(previous):
        history.confirm_write(
            lambda: history.index.put_open(
                task_open_row(
                    snapshot.handle,
                    owner.session_id,
                    snapshot.kind,
                    owner.human_id,
                    snapshot.created_at,
                )
            )
        )
    return history._session_state(owner.session_id)


def close_task(history: TaskHistory, previous: TaskSnapshot | None, snapshot: TaskSnapshot) -> None:
    """Remove a task's open record after the observation that settled it."""
    if history._global is None and needs_record(previous) and not needs_record(snapshot):
        _close(history, task_open_row(snapshot.handle, "", "", "", "")[0])


def task_obligation_settled(history: TaskHistory, task: TaskSnapshot) -> None:
    """The owner finished a terminal task's publication: its open record can go."""
    if history._global is None and task.terminal_at is not None:
        _close(history, task_open_row(task.handle, "", "", "", "")[0])


def join_session(history: TaskHistory, snapshot: TaskSnapshot) -> None:
    """Place a new handle's session in its parent's tree (or root a new tree)."""
    index = history.index
    session_id, human_id = snapshot.owner.session_id, snapshot.owner.human_id
    parent_root: str | None = None
    parent = snapshot.owner.parent_handle
    if parent is not None:
        row = index.handle(parent)
        if row is not None:
            parent_root = index.session_root(str(row["session_id"]))
        else:
            # The parent is not recorded yet: it adopts this session when it is.
            waiting = index.rows("wait.", parent)
            if session_id not in waiting:
                history.confirm_write(
                    lambda: index.put_lookup(list_row("wait.", parent, [*waiting, session_id]))
                )
    if not index.session_known(session_id):
        root = parent_root if parent_root is not None else session_id
        if root != session_id:
            members = index.members(root)
            history.confirm_write(
                lambda: index.put_lookup(members_row(root, [*members, session_id]))
            )
        history.confirm_write(lambda: index.put_lookup(session_row(session_id, root, human_id)))
        if root != session_id:
            _absorb(history, session_id, root)
    else:
        root = index.session_root(session_id)
        if parent_root is not None and parent_root != root:
            _merge(history, root, parent_root, human_id)
            root = parent_root
    roots = index.rows("hum.", human_id)
    if root not in roots:
        history.confirm_write(
            lambda: index.put_lookup(list_row("hum.", human_id, sorted({*roots, root})))
        )
    _adopt_waiting(history, snapshot.handle, root, human_id)


def _adopt_waiting(history: TaskHistory, handle: str, root: str, human_id: str) -> None:
    """Sessions recorded before their parent ``handle`` join its tree now that it exists."""
    index = history.index
    waiting = index.rows("wait.", handle)
    if not waiting:
        return
    for session_id in waiting:
        orphan_root = index.session_root(session_id)
        if orphan_root != root:
            _merge(history, orphan_root, root, human_id)
    history.confirm_write(lambda: index.put_lookup(list_row("wait.", handle, [])))


def _absorb(history: TaskHistory, session_id: str, root: str) -> None:
    """A session joined ``root``: carry over what its own log already held."""
    singleton = history.trees.cached(session_id) or load_tree(history, session_id, [session_id])
    tree = history.trees.cached(root)
    if singleton.holds_data() or tree is None:
        history.trees.invalidate(session_id, root)
        return
    history.trees.invalidate(session_id)
    tree.members.add(session_id)


def _merge(history: TaskHistory, old_root: str, new_root: str, human_id: str) -> None:
    """Lineage joined two trees: move every member of one under the other's root."""
    index = history.index
    moving = index.members(old_root)
    joined = index.members(new_root)
    history.confirm_write(lambda: index.put_lookup(members_row(new_root, [*joined, *moving])))
    for member in moving:
        history.confirm_write(partial(index.put_lookup, session_row(member, new_root, human_id)))
    history.trees.invalidate(old_root, new_root)
    logger.warning(
        "Task boundary reason=task_tree_merged from=%s into=%s members=%d",
        old_root,
        new_root,
        len(moving),
    )


# ---- mail, inputs, references, aliases, workflows -----------------------------


def open_mail(history: TaskHistory, item: MailItem) -> None:
    """Pending mail is listed before its acceptance is recorded."""
    if history._global is None:
        history.confirm_write(
            lambda: history.index.put_open(mail_open_row(item.recipient_session, item.id))
        )


def close_mail(history: TaskHistory, session_id: str, mail_ids: tuple[str, ...]) -> None:
    """Committed or withdrawn mail leaves the pending listing."""
    if history._global is None:
        for mail_id in mail_ids:
            _close(history, mail_open_row(session_id, mail_id)[0])


def open_display(history: TaskHistory, state: TreeState, record: RecipientInput) -> None:
    """Interactive mail about to be committed owes its owner a display repair."""
    if history._global is not None:
        return
    for mail_id in record.notification_ids:
        item = state.mail_item(record.session_id, mail_id)
        if item is not None and displayed(item):
            row = display_open_row(record.session_id, mail_id)
            history.confirm_write(partial(history.index.put_open, row))


def display_settled(history: TaskHistory, item: MailItem) -> None:
    """The owner projected a committed item's display state."""
    if history._global is None and displayed(item):
        _close(history, display_open_row(item.recipient_session, item.id)[0])


def open_reference(history: TaskHistory, context: SubmissionContext) -> None:
    """A worker reference is findable before its submission event."""
    if history._global is None:
        history.confirm_write(
            lambda: history.index.put_lookup(
                reference_row(context.reference, context.handle, context.owner.session_id)
            )
        )


def open_alias(history: TaskHistory, alias: TaskAlias) -> None:
    """A historical alias is findable before its event."""
    if history._global is None:
        rows = [row for row in history.index.rows("a.", alias.alias) if isinstance(row, dict)]
        rows.append(alias.model_dump(mode="json"))
        history.confirm_write(lambda: history.index.put_lookup(list_row("a.", alias.alias, rows)))


def open_workflow(history: TaskHistory, root_handle: str) -> None:
    """A checkpoint may leave its owner pointer behind until repaired."""
    if history._global is None:
        history.confirm_write(lambda: history.index.put_open(workflow_open_row(root_handle)))


def close_workflow(history: TaskHistory, root_handle: str) -> None:
    """The owner pointer matches its checkpoint again."""
    if history._global is None:
        _close(history, workflow_open_row(root_handle)[0])


# ---- reconciliation and eviction ----------------------------------------------


def _stale(history: TaskHistory, name: str, row: dict[str, object]) -> bool:
    if name.startswith("t~"):
        return not needs_record(history.task_snapshot(str(row.get("handle", ""))))
    if name.startswith("w~"):
        return history.task_snapshot(str(row.get("root_handle", ""))) is None
    session_id, mail_id = str(row.get("session_id", "")), str(row.get("mail_id", ""))
    state = history._session_state(session_id)
    if name.startswith("m~"):
        return mail_id not in state._pending.get(session_id, {})
    if name.startswith("d~"):
        return state.mail_item(
            session_id, mail_id
        ) is None or mail_id not in state._acknowledged.get(session_id, set())
    return False


def reconcile_open(history: TaskHistory, *, prefix: str = "", session_id: str | None = None) -> int:
    """Remove open records whose event never landed or whose obligation already closed.

    ``prefix`` limits it to one record kind (``"w~"``); ``session_id`` to the records of
    one conversation, so recovery reads only that conversation's trees. Runs under the
    writer lock, so no opening record is between its put and its event. Returns the
    number of records removed.
    """
    if history._global is not None or history.requires_recovery:
        return 0
    removed = 0
    with history._lock:
        for name, row in history.index.open_records(prefix):
            if session_id is not None and str(row.get("session_id", "")) != session_id:
                continue
            try:
                stale = _stale(history, name, row)
            except Exception as error:
                logger.exception(
                    "Task boundary reason=task_open_reconcile_failed record=%s error_type=%s",
                    name,
                    type(error).__name__,
                )
                continue
            if stale:
                _close(history, name)
                removed += 1
    trim(history)
    return removed


def _pinned(history: TaskHistory, tree: TreeState, busy: Callable[[str], bool] | None) -> bool:
    with history._listener_lock:
        if any(handle in history._handle_listeners for handle in tree._tasks):
            return True
        if any(member in history._recipient_listeners for member in tree.members):
            return True
    return busy is not None and any(busy(member) for member in tree.members)


def _evict(history: TaskHistory, tree: TreeState, busy: Callable[[str], bool] | None) -> bool:
    with history._lock, history._view_lock:
        if history.trees.cached(tree.root) is not tree or _pinned(history, tree, busy):
            return False
        history.trees.drop(tree.root)
    for member in sorted(tree.members):
        if busy is None or not busy(member):
            history.arc.release_session(member)
    return True


def release_session_tree(
    history: TaskHistory, session_id: str, *, busy: Callable[[str], bool] | None = None
) -> bool:
    """Drop the idle tree holding a session, and its members' ARC scopes.

    A tree stays while any member is busy, while a wait listens on one of its
    handles or recipients, or while a read view or write is in flight (the
    history locks are taken first). Returns whether a tree was dropped.
    """
    if history._global is not None:
        return False
    tree = history.trees.cached(history.index.session_root(session_id))
    dropped = tree is not None and _evict(history, tree, busy)
    trim(history, busy=busy)
    return dropped


def trim(history: TaskHistory, *, busy: Callable[[str], bool] | None = None) -> None:
    """Keep the cache bounded: drop least recently used unpinned trees."""
    for tree in history.trees.trees():
        if len(history.trees.trees()) <= MAX_CACHED_TREES:
            return
        _evict(history, tree, busy)
