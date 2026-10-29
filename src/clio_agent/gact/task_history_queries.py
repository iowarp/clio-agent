"""Tree routing and cross-tree reads for the task history façade.

Every per-session or per-handle read resolves its tree through the derived
lookup records and loads only that tree. Reads that span trees are served from
the small ``task_open`` listing (open work, pending mail, repairs) or, for a
human's whole history, from that human's own trees, loaded transiently.
"""

from __future__ import annotations

import heapq
import logging
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Any

from clio_agent.arc.task_types import (
    CoreReference,
    MailItem,
    RecipientInput,
    TaskAlias,
    TaskSnapshot,
)
from clio_agent.gact.task_index_writes import needs_record
from clio_agent.gact.task_tree_state import TreeState, load_tree

if TYPE_CHECKING:
    from clio_agent.gact.task_history import TaskHistory

logger = logging.getLogger(__name__)


# ---- routing -------------------------------------------------------------


def load_session_tree(self: TaskHistory, root: str) -> TreeState:
    """Load one tree from its members' logs; a failure is reported, never hidden."""
    try:
        return load_tree(self, root, self.index.members(root))
    except Exception as error:
        logger.exception(
            "Task boundary reason=task_tree_load_failed root=%s error_type=%s",
            root,
            type(error).__name__,
        )
        raise


def session_state(self: TaskHistory, session_id: str) -> TreeState:
    """The tree holding a session's task records, loaded on first use."""
    global_state = self._global
    if global_state is not None:
        return global_state
    return self.trees.ensure(self.index.session_root(session_id), self._load_tree)


def handle_state(self: TaskHistory, handle: str) -> TreeState | None:
    """The tree holding a handle, found through its lookup record."""
    global_state = self._global
    if global_state is not None:
        return global_state
    row = self.index.handle(handle)
    return self._session_state(str(row["session_id"])) if row is not None else None


def submission(self: TaskHistory, reference: str) -> CoreReference | None:
    """The confirmed content of an opaque worker reference."""
    global_state = self._global
    if global_state is not None:
        return global_state._submissions.get(reference)
    row = self.index.reference(reference)
    if row is None:
        return None
    return self._session_state(str(row["session_id"]))._submissions.get(reference)


def aliases(self: TaskHistory, value: str) -> tuple[TaskAlias, ...]:
    """Read confirmed historical mappings without exposing the mutable index."""
    global_state = self._global
    if global_state is not None:
        with self._view_lock:
            return tuple(global_state._aliases.get(value, {}).values())
    return tuple(TaskAlias.model_validate(row) for row in self.index.rows("a.", value))


def latest_input(self: TaskHistory, assignment_id: str, epoch: str | None) -> RecipientInput | None:
    """The latest committed input of an assignment, from the assignment's tree."""
    key = (assignment_id, epoch)
    state = self._handle_state(assignment_id)
    if state is not None:
        return state._latest_inputs.get(key)
    # An identity with no task record (it never had an observation) can only hold
    # inputs in a tree this process has loaded; Core has no other place to find it.
    for tree in self.trees.trees():
        found = tree._latest_inputs.get(key)
        if found is not None:
            return found
    return None


# ---- per-tree queries ------------------------------------------------------


def descendant_handles(self: TaskHistory, root_handle: str) -> set[str]:
    """Read one assignment and its confirmed children, excluding session siblings."""
    state = self._handle_state(root_handle)
    if state is None:
        return set()
    with self._view_lock:
        if root_handle not in state._tasks:
            return set()
        handles = {root_handle}
        pending = [root_handle]
        while pending:
            for child in state._children.get(pending.pop(), set()):
                if child not in handles:
                    handles.add(child)
                    pending.append(child)
        return handles


def direct_child_by_invocation(
    self: TaskHistory, root_handle: str, invocation_id: str, *, session_id: str, human_id: str
) -> tuple[TaskSnapshot, ...]:
    """Find exact direct children for a durable dispatch intent.

    The owner and invocation checks are repeated against immutable task snapshots; this
    query is for recovering a lost acceptance receipt, not for granting task access.
    """
    state = self._handle_state(root_handle)
    if state is None:
        return ()
    with self._view_lock:
        root = state._tasks.get(root_handle)
        if root is None or root.owner.session_id != session_id or root.owner.human_id != human_id:
            return ()
        matches = []
        for handle in state._children.get(root_handle, set()):
            child = state._tasks.get(handle)
            if (
                child is not None
                and child.owner.parent_handle == root_handle
                and child.owner.human_id == human_id
                and child.invocation_id == invocation_id
            ):
                matches.append(child)
        return tuple(matches)


def tree_handles(self: TaskHistory, session_id: str, *, active_only: bool = False) -> set[str]:
    """Return the stable session's work and delegated descendants, never its ancestors."""
    state = self._session_state(session_id)
    with self._view_lock:
        if active_only:
            return set(state._active_trees.get(session_id, ()))
        handles = set(state._session_tasks.get(session_id, set()))
        pending = list(handles)
        while pending:
            for child in state._children.get(pending.pop(), set()):
                if child not in handles:
                    handles.add(child)
                    pending.append(child)
        return handles


def session_handles(self: TaskHistory, session_id: str) -> set[str]:
    """Read the session's own handles from its tree."""
    state = self._session_state(session_id)
    with self._view_lock:
        return set(state._session_tasks.get(session_id, set()))


def session_tree_ids(self: TaskHistory, session_id: str, *, human_id: str = "") -> set[str]:
    """Read stable descendant addresses without enumerating their retained assignments."""
    state = self._session_state(session_id)
    with self._view_lock:
        return state.session_tree_ids(session_id, human_id=human_id)


def _indexed(
    state: TreeState,
    kind: str | None,
    human_id: str | None,
    session_id: str | None,
    active_only: bool,
) -> set[str]:
    indexes = (
        (kind, state._kind_tasks),
        (human_id, state._human_tasks),
        (session_id, state._session_tasks),
    )
    selections = [index.get(key, set()) for key, index in indexes if key is not None]
    if active_only:
        selections.append(state._active_tasks)
    if not selections:
        return set(state._tasks)
    return set.intersection(*sorted(selections, key=len))


def task_handles(
    self: TaskHistory,
    *,
    kind: str | None = None,
    human_id: str | None = None,
    session_id: str | None = None,
    active_only: bool = False,
) -> set[str]:
    """Read passive Core indexes; callers authorize before disclosing selected rows.

    With a session the answer comes from that session's tree. Without one it is
    the human's whole history when ``human_id`` is given (loading that human's
    trees), and otherwise the open work listed in ``task_open``: active tasks
    and terminal tasks whose publication is still owed.
    """
    global_state = self._global
    if session_id is not None or global_state is not None:
        state = self._session_state(session_id) if session_id is not None else global_state
        assert state is not None
        with self._view_lock:
            return _indexed(state, kind, human_id, session_id, active_only)
    if human_id is not None and not active_only:
        handles: set[str] = set()
        for tree in human_trees(self, human_id):
            handles |= _indexed(tree, kind, human_id, None, False)
        return handles
    return open_handles(self, kind=kind, human_id=human_id, active_only=active_only)


def open_handles(
    self: TaskHistory,
    *,
    kind: str | None = None,
    human_id: str | None = None,
    active_only: bool = False,
) -> set[str]:
    """Open work (and owed publications) from ``task_open``, verified against each tree."""
    global_state = self._global
    if global_state is not None:
        with self._view_lock:
            return {
                handle
                for handle, task in global_state._tasks.items()
                if needs_record(task)
                and (not active_only or task.terminal_at is None)
                and (kind is None or task.kind == kind)
                and (human_id is None or task.owner.human_id == human_id)
            }
    handles: set[str] = set()
    for _name, row in self.index.open_records("t~"):
        if kind is not None and row.get("kind") != kind:
            continue
        if human_id is not None and row.get("human_id") != human_id:
            continue
        handle = str(row.get("handle", ""))
        task = self.task_snapshot(handle)
        if task is None or not needs_record(task):
            continue  # dangling or stale hint; boot reconciliation removes it
        if active_only and task.terminal_at is not None:
            continue
        handles.add(handle)
    return handles


def resource_handles(self: TaskHistory, resource_id: str) -> set[str]:
    """Read owner-published resource associations, re-checked in each owning tree."""
    global_state = self._global
    if global_state is not None:
        with self._view_lock:
            return set(global_state._resource_tasks.get(resource_id, ()))
    handles: set[str] = set()
    for handle in self.index.rows("res.", resource_id):
        state = self._handle_state(str(handle))
        if state is not None and handle in state._resource_tasks.get(resource_id, ()):
            handles.add(str(handle))
    return handles


def human_trees(self: TaskHistory, human_id: str) -> list[TreeState]:
    """Every tree holding a human's tasks; uncached trees load transiently."""
    roots = sorted({self.index.session_root(root) for root in self.index.rows("hum.", human_id)})
    trees: list[TreeState] = []
    for root in roots:
        cached = self.trees.cached(root)
        if cached is not None:
            trees.append(cached)
            continue
        tree = self._load_tree(root)
        trees.append(tree)
        cached_members = {member for held in self.trees.trees() for member in held.members}
        for member in tree.members - cached_members:
            self.arc._segments.release(member)  # transient: keep no scopes resident
    return trees


def task_order(
    self: TaskHistory,
    domain: str,
    identity: str,
    *,
    human_id: str = "",
    kind: str = "",
    active_only: bool = False,
) -> Sequence[tuple[str, str]]:
    """An ascending (created_at, handle) ordering of one session, tree or human."""
    key = (domain, identity, human_id, kind, active_only)
    global_state = self._global
    if global_state is not None:
        with self._view_lock:
            return global_state._query_order.get(key, ())
    if domain != "human":
        state = self._session_state(identity)
        with self._view_lock:
            return state._query_order.get(key, ())
    if active_only:
        rows = []
        for handle in open_handles(self, kind=kind or None, human_id=identity, active_only=True):
            task = self.task_snapshot(handle)
            if task is not None:
                rows.append((task.created_at, handle))
        return sorted(rows)
    orders = [tree._query_order.get(key, ()) for tree in human_trees(self, identity)]
    return list(dict.fromkeys(heapq.merge(*orders)))


# ---- open-work listings ----------------------------------------------------


def pending_sessions(self: TaskHistory) -> tuple[str, ...]:
    """Recipients with accepted, undelivered mail, read from the ``m~`` open records alone.

    No conversation tree is loaded: the answer costs the open records, never history. A
    record that outlived its mail (``reconcile_open`` closes those) only costs one wake
    that finds nothing to deliver.
    """
    global_state = self._global
    if global_state is not None:
        with self._lock:
            return tuple(session for session, pending in global_state._pending.items() if pending)
    sessions = {str(row.get("session_id", "")) for _name, row in self.index.open_records("m~")}
    sessions.discard("")
    return tuple(sorted(sessions))


def acknowledged_mail(
    self: TaskHistory, kinds: tuple[str, ...], session_id: str | None = None
) -> tuple[MailItem, ...]:
    """Committed interactive mail whose display repair is still owed (of one session if given)."""
    global_state = self._global
    if global_state is not None:
        with self._lock:
            return tuple(
                item
                for sid, identities in global_state._acknowledged.items()
                if session_id is None or sid == session_id
                for identity in identities
                if (item := global_state._mail.get(sid, {}).get(identity)) is not None
                and item.kind in kinds
            )
    items: list[MailItem] = []
    for _name, row in self.index.open_records("d~"):
        owner, mail_id = str(row.get("session_id", "")), str(row.get("mail_id", ""))
        if session_id is not None and owner != session_id:
            continue
        state = self._session_state(owner)
        item = state.mail_item(owner, mail_id)
        if (
            item is not None
            and item.kind in kinds
            and mail_id in state._acknowledged.get(owner, set())
        ):
            items.append(item)
    return tuple(items)


def pending_workflow_references(self: TaskHistory) -> tuple[str, ...]:
    """Roots whose owner pointer may lag its latest checkpoint."""
    global_state = self._global
    if global_state is not None:
        with self._lock:
            return tuple(global_state._workflow_dirty)
    return tuple(
        str(row["root_handle"])
        for _name, row in self.index.open_records("w~")
        if row.get("root_handle")
    )


# ---- wake hints ------------------------------------------------------------


def listen(
    self: TaskHistory,
    callback: Callable[[int], None],
    *,
    handles: tuple[str, ...] | None = None,
    recipient_session: str | None = None,
) -> Callable[[], None]:
    """Register an eviction-safe wake hint; Core remains the pending-data authority."""
    with self._listener_lock:
        if handles is None:
            self._listeners.add(callback)
        else:
            for handle in handles:
                self._handle_listeners.setdefault(handle, set()).add(callback)
        if recipient_session is not None:
            self._recipient_listeners.setdefault(recipient_session, set()).add(callback)

    def remove() -> None:
        with self._listener_lock:
            self._listeners.discard(callback)
            for handle in handles or ():
                callbacks = self._handle_listeners.get(handle, set())
                callbacks.discard(callback)
                if not callbacks:
                    self._handle_listeners.pop(handle, None)
            if recipient_session is not None:
                callbacks = self._recipient_listeners.get(recipient_session, set())
                callbacks.discard(callback)
                if not callbacks:
                    self._recipient_listeners.pop(recipient_session, None)

    return remove


def listen_mail(self: TaskHistory, callback: Callable[[str], None]) -> Callable[[], None]:
    """Signal exact recipients after durable mailbox changes; payloads remain in Core."""
    with self._listener_lock:
        self._mail_listeners.add(callback)

    def remove() -> None:
        with self._listener_lock:
            self._mail_listeners.discard(callback)

    return remove


def notify_projected(
    self: TaskHistory, state: TreeState, event_type: str, payload: dict[str, Any]
) -> None:
    """Wake listeners after an online projection (never for replayed rows)."""
    handle = payload.get("handle")
    recipient = payload.get("recipient_session")
    mail_targets: tuple[str, ...] = ()
    if event_type == "session.coordination.grant":
        changed_grant = state._grants[str(payload["id"])]
        if "message" in changed_grant.rights:
            # Sending rights change on the grantee; retained peer input lives
            # in the target's mailbox and needs the same admission recheck.
            targets = {changed_grant.target_session}
            if changed_grant.target_handle is not None:
                target = self.task_snapshot(changed_grant.target_handle)
                targets = {target.owner.session_id} if target is not None else set()
            elif changed_grant.target_scope == "tree":
                targets.update(
                    self.session_tree_ids(
                        changed_grant.target_session, human_id=changed_grant.granting_human
                    )
                )
            mail_targets = tuple(
                sorted(
                    target for target in targets if self._session_state(target)._pending.get(target)
                )
            )
    notify(
        self,
        handle=handle,
        recipient=recipient,
        mail_changed=event_type.startswith("session.mail.")
        or event_type == "session.coordination.grant",
        mail_targets=mail_targets,
    )
    if event_type == "task.observed" and payload.get("fork_of"):
        # Whoever waits on the original waits on its forks (task_forks): wake them on
        # the fork's creation and on each change, all the way up the fork chain.
        from clio_agent.gact.task_forks import fork_ancestors  # noqa: PLC0415

        snapshot = self.task_snapshot(str(handle))
        for original in fork_ancestors(self, snapshot) if snapshot is not None else ():
            notify(self, handle=original)


def notify(
    self: TaskHistory,
    *,
    handle: Any = None,
    recipient: Any = None,
    restored: bool = False,
    mail_changed: bool = False,
    mail_targets: tuple[str, ...] = (),
) -> None:
    """Signal listeners once after a reset, rather than waking on historical rows."""
    with self._listener_lock:
        listeners = set(self._listeners)
        if restored:
            for callbacks in (
                *self._handle_listeners.values(),
                *self._recipient_listeners.values(),
            ):
                listeners.update(callbacks)
        else:
            listeners.update(self._handle_listeners.get(str(handle), set()))
            listeners.update(self._recipient_listeners.get(str(recipient), set()))
        mail_listeners = tuple(self._mail_listeners)
    for listener in listeners:
        try:
            listener(self.revision)
        except Exception as error:
            logger.exception(
                "Task boundary reason=task_wake_hint_failed error_type=%s", type(error).__name__
            )
            logger.warning("Task wake hint failed after Core commit: %s", type(error).__name__)
    changed_recipients = (str(recipient),) if recipient and mail_changed else ()
    # After a reset only a registered mailbox listener needs the pending recipients; at
    # startup none is registered yet, so the open records are listed once, by the
    # obligations lane's hold, not here as well.
    pending_recipients = (
        (self.pending_sessions() if mail_listeners else ())
        if restored
        else tuple(dict.fromkeys((*changed_recipients, *mail_targets)))
    )
    for pending_recipient in pending_recipients:
        for mail_listener in mail_listeners:
            try:
                mail_listener(pending_recipient)
            except Exception as error:
                logger.exception(
                    "Task boundary reason=mail_wake_hint_failed error_type=%s", type(error).__name__
                )
                logger.warning(
                    "Mailbox wake hint failed after Core commit: %s", type(error).__name__
                )
