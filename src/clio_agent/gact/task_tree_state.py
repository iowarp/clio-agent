"""One task tree's passive projection, loaded on demand from clio-core.

A task tree is every session joined by task lineage (``parent_handle``) under one
root session. Each task event is filed under a member session, so a tree is
rebuilt by reading only its members' task-event lanes
(:mod:`clio_agent.gact.task_event_lane`), never their whole event logs; no other
session is touched.
:class:`TreeCache` holds the trees the process is using and drops idle ones; the
records that find a tree live in clio-core (:mod:`clio_agent.gact.task_index_records`).
"""

from __future__ import annotations

import logging
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING, Any

from clio_agent.arc.task_types import (
    CoordinationGrant,
    CoreReference,
    MailItem,
    NativeAttempt,
    RecipientInput,
    TaskAlias,
    TaskSnapshot,
    TaskSubscription,
    WorkflowCheckpoint,
)
from clio_agent.gact import task_history_projection as projection
from clio_agent.gact.task_event_lane import TASK_EVENTS, session_task_events
from clio_agent.gact.task_lane_reads import INPUT_COMMITTED, receipt_record

if TYPE_CHECKING:
    from clio_agent.arc.memory import ARCMemory
    from clio_agent.gact.task_history import TaskHistory

logger = logging.getLogger(__name__)

_ENVELOPE = frozenset({"schema_version", "observation_id", "record"})


class TreeState:
    """The passive indexes of one tree (or, during the one-time build, of all sessions)."""

    def __init__(self, history: TaskHistory, root: str, members: Iterable[str]) -> None:
        self.history = history
        self.root = root
        self.members: set[str] = set(members)
        self.last_used = time.monotonic()
        self._replaying = False
        # Filing session per subscription (only the one-time build reads it).
        self.subscription_files: dict[tuple[str, str], str] = {}
        self._tasks: dict[str, TaskSnapshot] = {}
        self._aliases: dict[str, dict[str, TaskAlias]] = {}
        self._mail: dict[str, dict[str, MailItem]] = {}
        self._pending: dict[str, dict[str, MailItem]] = {}
        self._inputs: dict[tuple[str, str], RecipientInput] = {}
        self._latest_inputs: dict[tuple[str, str | None], RecipientInput] = {}
        self._notification_inputs: dict[tuple[str, str], str] = {}
        self._iterations: dict[tuple[str, str, str], str] = {}
        self._input_locations: dict[tuple[str, str], dict[str, Any]] = {}
        self._acknowledged: dict[str, set[str]] = {}
        self._observation_inputs: dict[tuple[str, str], set[str]] = {}
        self._withdrawn: dict[str, set[str]] = {}
        self._grants: dict[str, CoordinationGrant] = {}
        self._submissions: dict[str, CoreReference] = {}
        self._attempts: dict[str, dict[str, NativeAttempt]] = {}
        self._deduplicated: dict[tuple[str, str], str] = {}
        self._session_tasks: dict[str, set[str]] = {}
        self._kind_tasks: dict[str, set[str]] = {}
        self._human_tasks: dict[str, set[str]] = {}
        self._active_tasks: set[str] = set()
        self._query_order: dict[tuple[str, str, str, str, bool], list[tuple[str, str]]] = {}
        self._active_trees: dict[str, set[str]] = {}
        self._tree_sessions: dict[tuple[str, str], set[str]] = {}
        self._resource_tasks: dict[str, set[str]] = {}
        self._assignments: dict[str, str] = {}
        self._children: dict[str, set[str]] = {}
        self._recipient_grants: dict[str, set[str]] = {}
        self._subscriptions: dict[str, dict[str, TaskSubscription]] = {}
        self._workflow_bindings: dict[tuple[str, str], WorkflowCheckpoint] = {}
        self._workflow_executions: dict[tuple[str, str], WorkflowCheckpoint] = {}
        self._workflow_latest: dict[str, WorkflowCheckpoint] = {}
        self._workflow_dirty: set[str] = set()
        self._workflow_unresolved: dict[str, str] = {}

    @property
    def arc(self) -> ARCMemory:
        """The ARC whose event logs this tree projects."""
        return self.history.arc

    @property
    def revision(self) -> int:
        """The history-wide wake hint; replay never advances it per row."""
        return self.history.revision

    @revision.setter
    def revision(self, value: int) -> None:
        if not self._replaying:
            self.history.revision = value

    def read_content(self, reference: CoreReference) -> Any:
        """Read exact content through the history's validated reader.

        A tree may load on whichever thread first needs it; the store audits a
        read on the server loop instead of refusing it.
        """
        return self.history._read_content(reference)

    def context_inputs(self, session_id: str, scope: str) -> dict[str, RecipientInput]:
        """Read proven model inputs for one fixed Core context scope."""
        return projection._scope_inputs(self, session_id, scope)

    def acknowledged_notifications(self, session_id: str) -> frozenset[str]:
        """Committed notification identities of one member session."""
        return frozenset(self._acknowledged.get(session_id, ()))

    def mail_item(self, session_id: str, identity: str) -> MailItem | None:
        """One accepted mailbox header of a member session."""
        return self._mail.get(session_id, {}).get(identity)

    def mail_withdrawn(self, session_id: str, identity: str) -> bool:
        """Whether a member session's mail was withdrawn."""
        return identity in self._withdrawn.get(session_id, set())

    def session_tree_ids(self, session_id: str, *, human_id: str = "") -> set[str]:
        """Descendant sessions of a member session inside this tree."""
        return set(self._tree_sessions.get((session_id, human_id), ()))

    def holds_data(self) -> bool:
        """Whether any projection field is populated."""
        return any(getattr(self, name) for name in projection._PROJECTION_FIELDS)


def replay(
    state: TreeState,
    events: Iterable[tuple[str, dict[str, Any]]],
    *,
    owner_filed_subscriptions: bool,
) -> None:
    """Project confirmed task events into a fresh state, then derive orders and inputs.

    Args:
        state: An empty state to fill.
        events: ``(filing session, event content)`` pairs; sessions may come in any order.
        owner_filed_subscriptions: Project a subscription only from its task owner's log.
            Trees use this: the one-time build copied every subscription into its
            owner's log, so a copy filed under a subscriber elsewhere is historical.
    """
    state._replaying = True
    deferred: list[tuple[str, dict[str, Any]]] = []
    receipts: dict[tuple[str, str], dict[str, Any]] = {}
    for session_id, content in events:
        event_type = str(content.get("event_type", ""))
        if event_type not in TASK_EVENTS:
            continue
        payload = content.get("payload") or {}
        if not _ENVELOPE <= payload.keys():
            if "schema_version" in payload:
                raise ValueError("Task history observation envelope is incomplete")
            continue  # unversioned historical formats are decoded at startup migration
        if event_type == INPUT_COMMITTED:
            # A lane receipt: the input reached its context scope (read below, not here).
            receipts[(session_id, str(payload["record"].get("id")))] = payload
            continue
        if event_type == "task.subscription":
            record = payload["record"]
            if owner_filed_subscriptions:
                deferred.append((session_id, payload))
                continue
            key = (str(record.get("handle")), str(record.get("recipient_session")))
            state.subscription_files[key] = session_id
        snapshot = (
            projection._prepare_snapshot(state, payload) if event_type == "task.observed" else None
        )
        projection._project(state, event_type, payload, snapshot=snapshot)
    for session_id, payload in deferred:
        task = state._tasks.get(str(payload["record"].get("handle")))
        if task is not None and task.owner.session_id == session_id:
            projection._project(state, "task.subscription", payload)
    # Build from latest observations once, with complete parent identities.
    for snapshot in state._tasks.values():
        projection._index_task_order(state, snapshot, known=True)
    for ordering in state._query_order.values():
        ordering.sort()
    scopes: dict[tuple[str, str], dict[str, RecipientInput]] = {}
    for locator in state._input_locations.values():
        receipt = receipts.get((locator["session_id"], locator["id"]))
        if receipt is not None:
            recovered: RecipientInput | None = receipt_record(receipt, locator)
        else:  # no receipt (an older store, or a stop between commit and receipt)
            key = (locator["session_id"], locator["scope"])
            if key not in scopes:
                scopes[key] = state.context_inputs(*key)
            recovered = scopes[key].get(locator["id"])
        if recovered is not None:
            projection._accept_input(state, recovered)
    state._replaying = False


def load_tree(history: TaskHistory, root: str, members: Iterable[str]) -> TreeState:
    """Rebuild one tree from its member sessions' task-event lanes only."""
    state = TreeState(history, root, members)
    events = (
        event
        for session_id in sorted(state.members)
        for event in session_task_events(history.arc, session_id)
    )
    replay(state, events, owner_filed_subscriptions=True)
    return state


class _Pending:
    """One in-flight single-flight load."""

    def __init__(self, generation: int) -> None:
        self.generation = generation
        self.done = threading.Event()
        self.tree: TreeState | None = None
        self.error: BaseException | None = None


class TreeCache:
    """The trees this process is using: single-flight loads, LRU order, explicit drops.

    The cache lock is held only for dictionary updates, never across I/O, and a
    load needs neither the history writer lock nor its read-view lock, so a reader
    already holding the read view can wait for a load without a lock cycle.
    """

    def __init__(self, history: TaskHistory) -> None:
        self.history = history
        self._trees: OrderedDict[str, TreeState] = OrderedDict()
        self._loading: dict[str, _Pending] = {}
        self._lock = threading.Lock()
        self._generation = 0
        self.loads = 0

    def cached(self, root: str) -> TreeState | None:
        """A tree already in memory, without loading it."""
        with self._lock:
            return self._trees.get(root)

    def trees(self) -> list[TreeState]:
        """Every cached tree, least recently used first."""
        with self._lock:
            return list(self._trees.values())

    def ensure(self, root: str, load: Callable[[str], TreeState]) -> TreeState:
        """Return the cached tree, loading it once even under concurrent callers."""
        while True:
            with self._lock:
                tree = self._trees.get(root)
                if tree is not None:
                    self._trees.move_to_end(root)
                    tree.last_used = time.monotonic()
                    return tree
                pending = self._loading.get(root)
                owner = pending is None
                if pending is None:
                    pending = self._loading[root] = _Pending(self._generation)
            if not owner:
                pending.done.wait()
                if pending.error is not None:
                    raise pending.error
                if pending.tree is not None:
                    return pending.tree
                continue
            try:
                tree = load(root)
            except BaseException as error:
                with self._lock:
                    self._loading.pop(root, None)
                pending.error = error
                pending.done.set()
                raise
            with self._lock:
                self._loading.pop(root, None)
                installed = pending.generation == self._generation
                if installed:
                    self._trees[root] = tree
                    self.loads += 1
            pending.tree = tree if installed else None
            pending.done.set()
            if installed:
                return tree
            # A membership change or reset raced this load: read the tree again.

    def invalidate(self, *roots: str) -> None:
        """Drop trees whose membership changed; in-flight loads are discarded too."""
        with self._lock:
            for root in roots:
                self._trees.pop(root, None)
            self._generation += 1

    def drop(self, root: str) -> TreeState | None:
        """Remove one tree (eviction); the caller holds the history locks."""
        with self._lock:
            return self._trees.pop(root, None)

    def clear(self) -> None:
        """Drop every tree (reset)."""
        with self._lock:
            self._trees.clear()
            self._generation += 1
