"""AgentTask record + registry projection + lifecycle (#948 S2, #950).

A spawned child agent (S3+) is a real turn in a real child SESSION. The
``AgentTask`` is a *projection* over that child session — its authoritative fields
live on the child session's ``metadata`` (persisted in ``sessions.json`` with
``metadata.session_type == "agent_task"``), so there is **no fifth store** and the
projection is #737-forward-compatible: when the normalized event log lands, the
same record re-sources with no API change.

Three surfaces layer over that one truth:

* **Record** — :class:`AgentTask`, a frozen dataclass with a validated status
  lifecycle (``queued → running → completed|failed|cancelled``, plus a terminal
  ``consumed`` marker for async observe-later) and typed reason catalogs.
* **Registry** — :class:`AgentTaskRegistry` on ``app.state``: a dict + a per-parent
  index + one ``threading.Event`` per task (the S6 wait primitive), rebuilt at boot
  by folding ``session_type == "agent_task"`` sessions.
* **Live feed** — ``agent.task.*`` events published to BOTH the parent and child
  session channels (see :func:`publish_agent_task_event`).

The record vocabulary deliberately mirrors clio-relay's durable job records (status
lifecycle, timelines, artifact ref) so federation (#671) later swaps the executor
behind the seam, not the record model.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, field, replace
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from fastapi import FastAPI

    from clio_agent.gact.types import Session

logger = logging.getLogger(__name__)

# The metadata marker that makes a session an agent-task projection.
SESSION_TYPE_AGENT_TASK = "agent_task"

# Status lifecycle. ``consumed`` is not a status but a terminal marker (see
# ``consumed_at``); the five below are the queryable statuses.
STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"
STATUSES = frozenset(
    {STATUS_QUEUED, STATUS_RUNNING, STATUS_COMPLETED, STATUS_FAILED, STATUS_CANCELLED}
)
TERMINAL_STATUSES = frozenset({STATUS_COMPLETED, STATUS_FAILED, STATUS_CANCELLED})

# Legal status transitions. Anything not listed is rejected by ``transition`` —
# the record can never move backward out of a terminal state or skip ``running``
# into completion illegitimately.
LEGAL_TRANSITIONS: dict[str, frozenset[str]] = {
    STATUS_QUEUED: frozenset({STATUS_RUNNING, STATUS_CANCELLED, STATUS_FAILED}),
    STATUS_RUNNING: frozenset({STATUS_COMPLETED, STATUS_FAILED, STATUS_CANCELLED}),
    STATUS_COMPLETED: frozenset(),
    STATUS_FAILED: frozenset(),
    STATUS_CANCELLED: frozenset(),
}

# Typed reason catalogs (no free-form strings on the wire).
QUEUED_REASONS = frozenset({"concurrency_cap", "session_cap", "memory_pressure"})
ERROR_REASONS = frozenset(
    {
        "agent_error",
        "spawn_depth_exceeded",
        "child_requires_user_input",
        "cancelled_by_parent",
        "child_session_gone",
        "timeout",
    }
)


class AgentTaskError(ValueError):
    """A rejected AgentTask operation (illegal transition, unknown typed reason,
    wrong input). Carries a typed ``reason`` so callers surface it structurally."""

    def __init__(self, message: str, *, reason: str) -> None:
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True)
class AgentTask:
    """One spawned child task, projected from its child session's metadata.

    Immutable: lifecycle changes go through :meth:`AgentTaskRegistry.transition`,
    which produces a new record (``dataclasses.replace``) and re-persists it.
    """

    task_id: str
    parent_session_id: str
    child_session_id: str
    parent_turn_id: str = ""
    child_turn_id: str = ""
    agent_ref: dict[str, str] = field(default_factory=dict)  # {expert_id, blueprint_id}
    depth: int = 1
    # Per-run ensemble identity (#948 S5): when the SAME declared child is spawned N
    # times concurrently in one parent turn (an ensemble), each run gets its own task
    # record — ``run_index`` disambiguates them (0, 1, 2… in spawn order per
    # ``(parent_turn_id, child expert)``). It is a human-facing/attribution FIELD only;
    # per the S5 spike the ARC ``react_scope`` stays the bare agent id (a scope suffix
    # regresses the five consumers that treat scope as agent identity), so run identity
    # never leaks into scope. Durable on the record (assigned once at spawn, never
    # recomputed on queue admission).
    run_index: int = 0
    status: str = STATUS_QUEUED
    queued_reason: str = ""
    error_reason: str = ""
    created_at: str = ""
    updated_at: str = ""
    # ``result``: {message_ref, answer_excerpt (bounded), workflow_state}.
    result: Optional[dict[str, Any]] = None
    # RESERVED — the artifacts campaign (#670) fills this with a spill ref when a
    # child's output is large; carried from day one so federation records match.
    artifact_ref: str = ""
    # Async observe-later (#948 S6/S8): completed-but-unconsumed tasks set
    # ``notify_pending``; the parent's next turn consumes them (``consumed_at``).
    notify_pending: bool = False
    consumed_at: str = ""
    # Once-per-task terminal-event guard (#948 S4 adversarial-review fix): set the
    # first time a waiter emits this task's ``blueprint.delegation.completed``/
    # ``.failed`` wire event, so a re-wait (partial-timeout re-collect, id repeated
    # in a batch) never re-emits it (the server owns the de-duplicated stream). The
    # RESULT ROW is still returned on every wait; only the EVENT is once. Persisted
    # to the child-session metadata so a boot-rebuilt registry does not re-emit.
    delegation_reported: bool = False

    def to_metadata(self) -> dict[str, Any]:
        """The child-session metadata block that is the authoritative store."""

        return {"session_type": SESSION_TYPE_AGENT_TASK, "agent_task": asdict(self)}

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    @classmethod
    def from_session(cls, session: "Session") -> Optional["AgentTask"]:
        """Project an :class:`AgentTask` from a child session, or ``None`` when the
        session is not an agent-task projection / carries no task block."""

        meta = getattr(session, "metadata", None) or {}
        if meta.get("session_type") != SESSION_TYPE_AGENT_TASK:
            return None
        block = meta.get("agent_task")
        if not isinstance(block, Mapping):
            return None
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in block.items() if k in known})


class AgentTaskRegistry:
    """In-memory projection of agent tasks over the session store.

    A dict (task_id → record), a per-parent index, and one
    :class:`threading.Event` per task (set on terminal transition — the S6 wait
    primitive). Rebuilt at boot from the session store. All mutations go through
    the store first (authoritative) then update the projection, so a restart
    re-derives the same registry.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._tasks: dict[str, AgentTask] = {}
        self._by_parent: dict[str, set[str]] = {}
        self._events: dict[str, threading.Event] = {}

    def _index(self, task: AgentTask) -> None:
        self._tasks[task.task_id] = task
        self._by_parent.setdefault(task.parent_session_id, set()).add(task.task_id)
        self._events.setdefault(task.task_id, threading.Event())
        if task.is_terminal:
            self._events[task.task_id].set()

    def register(self, task: AgentTask) -> AgentTask:
        """Add (or replace) a task in the projection. Idempotent by ``task_id``."""

        with self._lock:
            self._index(task)
            return task

    def get(self, task_id: str) -> Optional[AgentTask]:
        with self._lock:
            return self._tasks.get(task_id)

    def snapshot(self) -> list[AgentTask]:
        """A consistent point-in-time list of every task (for cap/queue scans)."""

        with self._lock:
            return list(self._tasks.values())

    def for_parent(self, parent_session_id: str) -> list[AgentTask]:
        """All tasks spawned by ``parent_session_id``, newest-created first."""

        with self._lock:
            ids = self._by_parent.get(parent_session_id, set())
            tasks = [self._tasks[t] for t in ids if t in self._tasks]
        return sorted(tasks, key=lambda t: t.created_at, reverse=True)

    def event(self, task_id: str) -> threading.Event:
        """The completion Event for ``task_id`` (created on demand). Set when the
        task reaches a terminal status — the primitive a waiting parent blocks on."""

        with self._lock:
            return self._events.setdefault(task_id, threading.Event())

    def transition(
        self,
        task_id: str,
        new_status: str,
        *,
        error_reason: str = "",
        result: Optional[dict[str, Any]] = None,
        notify_pending: Optional[bool] = None,
        updated_at: str = "",
    ) -> AgentTask:
        """Move a task to ``new_status`` with validation, returning the new record.

        Rejects unknown statuses, illegal transitions, and an ``error_reason`` /
        failure without a typed reason. Sets the completion Event on a terminal
        transition. The caller is responsible for persisting the returned record to
        the child session's metadata (authoritative) — see ``persist_agent_task``.
        """

        if new_status not in STATUSES:
            raise AgentTaskError(f"unknown status {new_status!r}", reason="unknown_status")
        if error_reason and error_reason not in ERROR_REASONS:
            raise AgentTaskError(
                f"unknown error_reason {error_reason!r}", reason="unknown_error_reason"
            )
        with self._lock:
            current = self._tasks.get(task_id)
            if current is None:
                raise AgentTaskError(f"unknown task {task_id!r}", reason="unknown_task")
            # A terminal record is IMMUTABLE. Guard here BEFORE the same-status
            # shortcut below, else a same-status re-transition (completed→completed)
            # would skip the empty allowed-set and silently clobber result / re-fire
            # the wait-Event — defeating the terminal-finality the wait/consume
            # design depends on. (Legitimate terminal mutation — consume — has its
            # own method, mark_consumed.)
            if current.is_terminal:
                raise AgentTaskError(
                    f"task {task_id!r} is already terminal (status={current.status!r}); "
                    "terminal records are immutable",
                    reason="already_terminal",
                )
            allowed = LEGAL_TRANSITIONS.get(current.status, frozenset())
            if new_status != current.status and new_status not in allowed:
                raise AgentTaskError(
                    f"illegal transition {current.status!r} -> {new_status!r}",
                    reason="illegal_transition",
                )
            if new_status == STATUS_FAILED and not error_reason and not current.error_reason:
                raise AgentTaskError(
                    "failed transition requires a typed error_reason",
                    reason="missing_error_reason",
                )
            updates: dict[str, Any] = {"status": new_status}
            if updated_at:
                updates["updated_at"] = updated_at
            if error_reason:
                updates["error_reason"] = error_reason
            if result is not None:
                updates["result"] = result
            if notify_pending is not None:
                updates["notify_pending"] = notify_pending
            updated = replace(current, **updates)
            self._index(updated)
            return updated

    def mark_consumed(self, task_id: str, consumed_at: str) -> AgentTask:
        """Mark an async task's result consumed by the parent's next turn (clears
        ``notify_pending``). Only terminal tasks can be consumed."""

        with self._lock:
            current = self._tasks.get(task_id)
            if current is None:
                raise AgentTaskError(f"unknown task {task_id!r}", reason="unknown_task")
            if not current.is_terminal:
                raise AgentTaskError(
                    f"cannot consume a non-terminal task (status={current.status!r})",
                    reason="not_terminal",
                )
            updated = replace(current, notify_pending=False, consumed_at=consumed_at)
            self._index(updated)
            return updated

    def mark_delegation_reported(self, task_id: str) -> Optional[AgentTask]:
        """Atomically claim a task's once-per-task terminal-event emission.

        Returns the updated record the FIRST time it is called for ``task_id`` (the
        caller emits the ``blueprint.delegation.*`` wire event and persists the
        record so the flag survives a boot rebuild); returns ``None`` on every
        subsequent call — the event was already emitted and must NOT be re-emitted
        (the server owns the de-duplicated stream). Returns ``None`` for an unknown
        id. The check-and-set is under the registry lock so two concurrent waiters
        on the same terminal task race to emit exactly once.
        """

        with self._lock:
            current = self._tasks.get(task_id)
            if current is None or current.delegation_reported:
                return None
            updated = replace(current, delegation_reported=True)
            self._index(updated)
            return updated

    def rebuild_from_sessions(self, sessions: Iterable["Session"]) -> int:
        """Fold every ``session_type == "agent_task"`` session into the projection
        (boot recovery). Returns the number of tasks folded."""

        with self._lock:
            self._tasks.clear()
            self._by_parent.clear()
            self._events.clear()
            n = 0
            for session in sessions:
                # A single malformed / schema-drifted agent_task block must NOT
                # brick server boot — skip it with a typed reason, mirroring
                # SessionStore._load's per-row resilience (the #737 forward-compat
                # goal). from_session raises TypeError on a block missing a required
                # field (hand-edit, partial store, cross-release rename).
                try:
                    task = AgentTask.from_session(session)
                except Exception as exc:  # noqa: BLE001 - one bad row must not fail boot
                    logger.warning(
                        "agent_task fold skipped reason=malformed_agent_task session=%s err=%r",
                        getattr(session, "id", "?"),
                        exc,
                    )
                    continue
                if task is not None:
                    self._index(task)
                    n += 1
            return n


def install_agent_task_registry(app: "FastAPI") -> AgentTaskRegistry:
    """Create the registry, fold existing agent-task sessions into it, and stash it
    on ``app.state.agent_task_registry``. Call once from ``build_app`` after the
    session store exists (boot recovery of the projection)."""

    registry = AgentTaskRegistry()
    registry.rebuild_from_sessions(app.state.sessions.list())
    app.state.agent_task_registry = registry
    return registry


def seed_agent_task(
    app: "FastAPI",
    *,
    parent_session_id: str,
    agent_ref: Mapping[str, str],
    parent_turn_id: str = "",
    depth: int = 1,
    task_id: str = "",
    status: str = STATUS_QUEUED,
) -> AgentTask:
    """Mint a child session + its AgentTask projection, persist, register, and
    publish the initial lifecycle event.

    This is the S2 SEAM: it creates the durable child session exactly as S3's real
    ``spawn_child_turn`` will (``parent_session_id`` lineage, ``session_type`` marker),
    minus the actual turn. S3 replaces the caller, not the record model. Tests and the
    live-gate probe use it to seed a task without a running child.
    """

    import uuid  # noqa: PLC0415
    from datetime import datetime, timezone  # noqa: PLC0415

    now = datetime.now(timezone.utc).isoformat()
    tid = task_id or ("task_" + uuid.uuid4().hex[:12])
    parent = app.state.sessions.get(parent_session_id)
    workspace_id = (
        getattr(parent, "workspace_id", "ws_default") if parent is not None else "ws_default"
    )
    child = app.state.sessions.create(
        workspace_id=workspace_id,
        title=f"agent-task {tid[-6:]}",
        parent_session_id=parent_session_id,
        agent={"id": agent_ref.get("expert_id", "")},
    )
    task = AgentTask(
        task_id=tid,
        parent_session_id=parent_session_id,
        child_session_id=child.id,
        parent_turn_id=parent_turn_id,
        agent_ref=dict(agent_ref),
        depth=depth,
        status=status,
        created_at=now,
        updated_at=now,
    )
    persist_agent_task(app, task)
    publish_agent_task_event(app, task, AGENT_TASK_EVENTS.get(status, "agent.task.queued"))
    return task


def persist_agent_task(app: "FastAPI", task: AgentTask) -> None:
    """Write a task's authoritative fields to its child session's metadata (the
    real store) AND update the in-memory projection. The registry is only ever a
    view over this; a restart re-derives it from the persisted metadata."""

    result = app.state.sessions.update(task.child_session_id, metadata_patch=task.to_metadata())
    if result is None:
        # No-silent-fallback: the AUTHORITATIVE store write no-op'd (the child
        # session is gone). Do NOT update the projection to a state the store can't
        # back — emit a typed reason and refuse, so the registry never diverges
        # from the persisted truth (a boot fold would silently drop the task).
        logger.warning(
            "agent_task persist skipped reason=child_session_gone task=%s child=%s",
            task.task_id,
            task.child_session_id,
        )
        raise AgentTaskError(
            f"child session {task.child_session_id!r} is gone; task {task.task_id!r} not persisted",
            reason="child_session_gone",
        )
    app.state.agent_task_registry.register(task)


# Ordered event vocabulary for the live feed, one per lifecycle edge.
AGENT_TASK_EVENTS = {
    STATUS_QUEUED: "agent.task.queued",
    STATUS_RUNNING: "agent.task.started",
    STATUS_COMPLETED: "agent.task.completed",
    STATUS_FAILED: "agent.task.failed",
    STATUS_CANCELLED: "agent.task.cancelled",
}
AGENT_TASK_CONSUMED_EVENT = "agent.task.consumed"


def publish_agent_task_event(app: "FastAPI", task: AgentTask, event_type: str) -> None:
    """Publish an ``agent.task.*`` event to BOTH the parent and child session
    channels (the bus is per-session), so a parent watching its SSE stream sees its
    children's lifecycle and a child's own stream carries it too.

    These are OPERATIONAL events — like ``message.created`` / ``session.status_changed``
    they go straight to the bus, NOT through ``_emit_semantic_event``: the task's
    durable state is its child-session metadata (the authoritative store), so the
    event carries no ARC dependency and can never crash a seed/cancel on a server
    whose semantic sink is wired but whose ARC is not yet ready.
    """

    from clio_agent.gact.events import Event  # noqa: PLC0415 - avoid import cycle

    payload = asdict(task)
    for sid in (task.parent_session_id, task.child_session_id):
        if not sid:
            continue
        app.state.bus.publish(Event(type=event_type, session_id=sid, payload=payload))
