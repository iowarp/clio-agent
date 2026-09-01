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
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Optional

from clio_agent.gact.runtime.permission_policies import inherit_child_session_policies

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
        "custom_agent_tools_unavailable",
        "spawn_depth_exceeded",
        # #1113: an unattended child that pauses for input no longer FAILS with
        # ``child_requires_user_input`` — its question is forwarded to the parent's
        # HITL surface (elicitation_bridge). The forward binds to the task, so its
        # edges terminate typed: no pending question to forward, a headless-parent
        # deadline, and a parent cancel/decline each fail the task with these.
        "child_question_forward_failed",
        "child_forward_unattended_timeout",
        "child_forward_declined",
        "child_forward_not_resumable",
        "cancelled_by_parent",
        "child_session_gone",
        "timeout",
        # #948 S6 adversarial-review [10]: a task folded at boot in a non-terminal
        # status has no live turn to resume (a crash/unclean stop left it mid-run),
        # so the boot settle fails it with this typed reason + notify_pending so the
        # parent's next turn learns its spawned task was interrupted and decides.
        "server_restart_interrupted",
    }
)


def display_run_name(agent_id: str, run_index: int, run_label: str) -> str:
    """The ONE server-side display-name rule for a spawned run (wire semantics).

    ``run_label`` wins when the task carries one; otherwise
    ``"{agent_id} #{run_index + 1}"``. Shared by every wire surface that needs a
    human-facing name for a task (``wait_agent_tasks``'s resolved
    ``waited_tasks``/structured ``results`` rows) so the rule lives in exactly
    ONE place — the server decides, the UI never infers.
    """

    return run_label or f"{agent_id} #{run_index + 1}"


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
    # Fan-out admission bound (#948 S5): when > 0, at most this many of the SAME
    # parent-expert's concurrent children at this depth may RUN before a spawn queues
    # (the declaring parent's ``fanout.max_workers``). Durable on the record so queue
    # admission (``_admit_next_queued``) honors the bound after a boot rebuild too; 0
    # means only the global per-depth cap applies.
    fanout_bound: int = 0
    # Fan-out GROUP identity (wire semantics, P5): minted ONCE per
    # ``spawn_agents_parallel`` call and stamped on every sibling spawned in that
    # call, so the wire carries explicit grouping instead of the UI inferring it
    # by adjacency/timing (the clean-wire rule: the SERVER emits display
    # semantics). Empty for a single ``spawn_agent_task`` spawn and for declared
    # ``run_workflow`` steps — never invented; absent, not a null/empty sentinel
    # value the UI would have to special-case. Durable on the record (assigned
    # once at spawn) so it survives the queued->running->completed lifecycle and
    # a boot rebuild, and rides ``TaskHandle``/``TaskResult`` across the invoker
    # boundary to reach the completed ``expert_handoff`` Part at wait-time.
    spawn_group_id: str = ""
    # The batch's total spawn count (>= 1) when ``spawn_group_id`` is set; 0 when
    # absent (mirrors ``spawn_group_id``'s presence, never invented independently).
    group_size: int = 0
    # P2.10 (#1127): additive run-handle vocabulary. These fields remain on the
    # authoritative child-session record so local and relay runs project identically.
    handle_id: str = ""
    run_label: str = ""
    live_state: str = ""
    host: str = "local"
    placement: str = "local"
    detached: bool = False
    dismissed: bool = False
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

    def __post_init__(self) -> None:
        """Keep lifecycle-backed live state canonical at record construction."""

        if self.status != STATUS_RUNNING or not self.live_state or self.live_state in STATUSES:
            object.__setattr__(self, "live_state", self.status)

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
        artifact_ref: Optional[str] = None,
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
            updates: dict[str, Any] = {"status": new_status, "live_state": new_status}
            if updated_at:
                updates["updated_at"] = updated_at
            if error_reason:
                updates["error_reason"] = error_reason
            if result is not None:
                updates["result"] = result
            if artifact_ref is not None:
                updates["artifact_ref"] = artifact_ref
            if notify_pending is not None:
                updates["notify_pending"] = notify_pending
            updated = replace(current, **updates)
            self._index(updated)
            return updated

    def mark_consumed(self, task_id: str, consumed_at: str) -> Optional[AgentTask]:
        """Atomically claim an async task's observe-later notification (clears
        ``notify_pending``, stamps ``consumed_at``), returning the updated record.

        The claim is a check-and-set under the registry lock — mirroring
        :meth:`mark_delegation_reported` — so two concurrent consumers
        (``wait_agent_tasks`` / ``check_agent_tasks`` / next-turn injection) race to
        claim exactly once: the FIRST call on a terminal, still-``notify_pending``
        task flips the flag and returns the record; every LATER call returns ``None``
        (already consumed) with no restamp, so :func:`consume_notification` never
        double-publishes ``agent.task.consumed``. An unknown id or a non-terminal
        task is a caller error (never a race) and raises ``AgentTaskError``."""

        with self._lock:
            current = self._tasks.get(task_id)
            if current is None:
                raise AgentTaskError(f"unknown task {task_id!r}", reason="unknown_task")
            if not current.is_terminal:
                raise AgentTaskError(
                    f"cannot consume a non-terminal task (status={current.status!r})",
                    reason="not_terminal",
                )
            if not current.notify_pending:
                # Already claimed by a concurrent/earlier consumer — the once-guard.
                return None
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


def resolve_waited_task_rows(app: "FastAPI", task_ids: Iterable[str]) -> list[dict[str, Any]]:
    """Resolve each ``task_id``'s DISPLAY row from the registry AT CALL TIME.

    The clean-wire rule for ``wait_agent_tasks``: the tool_call Part carries
    ``metadata.waited_tasks`` — one row per requested id —
    ``{task_id, agent_id, run_index, run_label, child_session_id, name}`` —
    so the UI never has to re-derive a display name from a raw task-id array.
    ``name`` is :func:`display_run_name`. Static task identity (agent, run
    index, run label, child session) is already durable on the record at
    spawn time, so this is resolvable BEFORE the wait ever blocks — it does
    not wait for or depend on the outcome.

    An id the registry does not (yet, or no longer) know still yields a row —
    never silently dropped from the array — with empty resolved fields and
    ``name`` falling back to the raw id (the best the server can say).
    """

    reg = getattr(app.state, "agent_task_registry", None)
    rows: list[dict[str, Any]] = []
    for tid in task_ids:
        task = reg.get(tid) if reg is not None else None
        if task is None:
            rows.append(
                {
                    "task_id": tid,
                    "agent_id": "",
                    "run_index": 0,
                    "run_label": "",
                    "child_session_id": "",
                    "name": tid,
                }
            )
            continue
        agent_id = task.agent_ref.get("expert_id", "")
        rows.append(
            {
                "task_id": task.task_id,
                "agent_id": agent_id,
                "run_index": task.run_index,
                "run_label": task.run_label,
                "child_session_id": task.child_session_id,
                "name": display_run_name(agent_id, task.run_index, task.run_label),
            }
        )
    return rows


#: Default ceiling on descendant-session traversal (:func:`descendant_session_ids`).
#: Spawn depth is already bounded (``spawn_depth_exceeded``), but the aggregation
#: walk carries its own cap so a pathological / cyclic task graph can never loop
#: unboundedly. ``1`` restricts to direct children only.
_DEFAULT_DESCENDANT_DEPTH = 8


def child_session_ids(app: "FastAPI", parent_session_id: str) -> list[str]:
    """Return the direct child session ids a parent spawned (via the task registry).

    Each :class:`AgentTask` carries the ``child_session_id`` of the real child
    SESSION it projects (#948 S2 substrate). Empty when the registry is absent or
    the session spawned nothing.
    """
    reg = getattr(app.state, "agent_task_registry", None)
    if reg is None:
        return []
    out: list[str] = []
    for task in reg.for_parent(parent_session_id):
        child = str(getattr(task, "child_session_id", "") or "")
        if child:
            out.append(child)
    return out


def descendant_session_ids(
    app: "FastAPI", root_session_id: str, *, max_depth: int = _DEFAULT_DESCENDANT_DEPTH
) -> list[str]:
    """Return the descendant child session ids of ``root_session_id`` (BFS, bounded).

    Walks the agent-task registry's per-parent index breadth-first: direct children
    at depth 1, their children at depth 2, and so on up to ``max_depth`` (``1`` =
    children only). The root is NOT included; each descendant appears once (a
    ``seen`` set makes a repeated / cyclic task graph terminate). Order is
    breadth-first, siblings newest-created first (the registry's ``for_parent``
    order). This is the substrate for parent-orchestrator provenance aggregation
    (GAP B, S5 #971): a parent session whose children executed the tools can merge
    their transform/artifact records with per-row session attribution.
    """
    reg = getattr(app.state, "agent_task_registry", None)
    if reg is None or max_depth < 1:
        return []
    out: list[str] = []
    seen: set[str] = {root_session_id}
    frontier = [root_session_id]
    depth = 0
    while frontier and depth < max_depth:
        next_frontier: list[str] = []
        for parent in frontier:
            for task in reg.for_parent(parent):
                child = str(getattr(task, "child_session_id", "") or "")
                if not child or child in seen:
                    continue
                seen.add(child)
                out.append(child)
                next_frontier.append(child)
        frontier = next_frontier
        depth += 1
    return out


def install_agent_task_registry(app: "FastAPI") -> AgentTaskRegistry:
    """Create the registry, fold existing agent-task sessions into it, and stash it
    on ``app.state.agent_task_registry``. Call once from ``build_app`` after the
    session store exists (boot recovery of the projection).

    After the fold, settle any INTERRUPTED tasks (#948 S6 adversarial-review [10]):
    a folded task still in a non-terminal status has no live turn on this fresh
    process, so it is a zombie — fail it typed + mark it observe-later pending."""

    registry = AgentTaskRegistry()
    registry.rebuild_from_sessions(app.state.sessions.list())
    app.state.agent_task_registry = registry
    settle_interrupted_agent_tasks(app)
    return registry


def settle_interrupted_agent_tasks(app: "FastAPI") -> int:
    """Fail every boot-folded task left in a NON-TERMINAL status (#948 S6 [10]).

    A ``queued``/``running`` task in the rebuilt registry was interrupted by an
    unclean stop (crash / power loss / SIGKILL): its in-flight turn does not exist
    on this fresh process and cannot be resumed, so leaving it non-terminal makes it
    a permanent zombie — ``wait_agent_tasks`` blocks its full budget on a never-set
    Event, ``check_agent_tasks`` reports it ``running`` forever, and it permanently
    counts against the per-depth concurrency cap (progressive slot starvation across
    restarts). Each such task is transitioned to ``failed`` with the typed reason
    ``server_restart_interrupted`` and ``notify_pending=True``, so the parent's next
    turn LEARNS its spawned task was interrupted (observe-later) and the model
    decides how to proceed. Failing them (terminal) also frees their per-depth slots
    — the running-task cap accounting counts only ``STATUS_RUNNING``. Returns the
    number settled. Idempotent: a second boot finds nothing non-terminal to settle.
    """

    reg = app.state.agent_task_registry
    now = datetime.now(timezone.utc).isoformat()
    settled = 0
    for task in reg.snapshot():
        if task.is_terminal:
            continue
        try:
            updated = reg.transition(
                task.task_id,
                STATUS_FAILED,
                error_reason="server_restart_interrupted",
                notify_pending=True,
                updated_at=now,
            )
        except AgentTaskError as exc:
            # A concurrent boot path already settled it, or an illegal-transition
            # edge — never brick boot; surface the typed reason.
            logger.warning(
                "agent_task boot settle skipped reason=%s task=%s",
                getattr(exc, "reason", "unknown"),
                task.task_id,
            )
            continue
        try:
            persist_agent_task(app, updated)
        except (AgentTaskError, OSError) as exc:
            logger.warning(
                "agent_task boot settle not persisted reason=%s task=%s err=%r",
                getattr(exc, "reason", type(exc).__name__),
                task.task_id,
                exc,
            )
        publish_agent_task_event(app, updated, AGENT_TASK_EVENTS[STATUS_FAILED])
        settled += 1
    if settled:
        logger.warning("agent_task boot settle failed %d interrupted task(s)", settled)
    return settled


def seed_agent_task(
    app: "FastAPI",
    *,
    parent_session_id: str,
    agent_ref: Mapping[str, str],
    parent_turn_id: str = "",
    depth: int = 1,
    task_id: str = "",
    status: str = STATUS_QUEUED,
    workspace_id: str | None = None,
    session_mode: str | None = None,
    session_scope_metadata: Mapping[str, Any] | None = None,
    run_index: int = 0,
    fanout_bound: int = 0,
    queued_reason: str = "",
    placement: str = "local",
    host: str = "",
    run_label: str = "",
    spawn_group_id: str = "",
    group_size: int = 0,
) -> AgentTask:
    """Mint a child session + its AgentTask projection, persist, register, and
    publish the initial lifecycle event.

    This is the S2 SEAM: it creates the durable child session exactly as S3's real
    ``spawn_child_turn`` will (``parent_session_id`` lineage, ``session_type`` marker),
    minus the actual turn. S3 replaces the caller, not the record model. Tests and the
    live-gate probe use it to seed a task without a running child.
    """

    now = datetime.now(timezone.utc).isoformat()
    tid = task_id or ("task_" + uuid.uuid4().hex[:12])
    parent = app.state.sessions.get(parent_session_id)
    child_workspace_id = (
        workspace_id
        if workspace_id is not None
        else (getattr(parent, "workspace_id", "ws_default") if parent is not None else "ws_default")
    )
    child = app.state.sessions.create(
        workspace_id=child_workspace_id,
        title=f"agent-task {tid[-6:]}",
        parent_session_id=parent_session_id,
        metadata={"spawn_placement": placement, **dict(session_scope_metadata or {})},
        agent={"id": agent_ref.get("expert_id", ""), "mode": "subagent"},
        mode=session_mode or getattr(parent, "mode", "edit"),
        approval_mode=getattr(parent, "approval_mode", "ask"),
    )
    inherit_child_session_policies(app, parent_session_id, child.id)
    task = AgentTask(
        task_id=tid,
        parent_session_id=parent_session_id,
        child_session_id=child.id,
        parent_turn_id=parent_turn_id,
        agent_ref=dict(agent_ref),
        depth=depth,
        run_index=run_index,
        fanout_bound=fanout_bound,
        spawn_group_id=spawn_group_id,
        group_size=group_size,
        handle_id=tid,
        run_label=run_label or f"{agent_ref.get('expert_id', 'agent')} #{run_index + 1}",
        live_state=status,
        host=host or (placement.split(":", 1)[1] if placement.startswith("relay:") else "local"),
        placement=placement,
        status=status,
        queued_reason=queued_reason,
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


def pending_notifications(app: "FastAPI", parent_session_id: str) -> list[AgentTask]:
    """Terminal, still-``notify_pending`` tasks a parent spawned async and has NOT
    yet consumed (via wait/check/injection), oldest-completed first (#948 S6).

    This is the observe-later feed the parent's NEXT turn drains: an async child
    that finished after (or during) the spawning turn sets ``notify_pending`` at
    completion; it stays pending until one of the three consumers flips it. Ordered
    by ``updated_at`` ascending so the earliest-finished result is presented first.
    """

    reg = app.state.agent_task_registry
    pending = [t for t in reg.for_parent(parent_session_id) if t.is_terminal and t.notify_pending]
    return sorted(pending, key=lambda t: t.updated_at)


def consume_notification(app: "FastAPI", task_id: str) -> Optional[AgentTask]:
    """Mark ONE async task's result consumed — exactly once (#948 S6).

    Shared by the three consumers (``wait_agent_tasks`` / ``check_agent_tasks`` /
    next-turn injection). The claim is ATOMIC: :meth:`AgentTaskRegistry.mark_consumed`
    does the ``notify_pending`` check-and-set under the registry lock (like
    ``mark_delegation_reported``), so exactly one caller flips ``notify_pending``
    off + stamps ``consumed_at`` and returns the record; a concurrent/later caller
    gets ``None`` and this function no-ops (no duplicate ``agent.task.consumed`` and
    no restamped ``consumed_at``). The claimant persists the record (durable across
    a boot rebuild, like ``delegation_reported``) and publishes the event. Returns
    the consumed record, or ``None`` when the task is unknown, non-terminal, or
    already consumed.

    Persistence is best-effort (never crashes the turn): a gone child session
    (``AgentTaskError``) or a transient store IO fault (``OSError`` from the disk
    flush) is logged with a typed reason and swallowed. Redelivery semantics are
    honest at-least-once ACROSS A CRASH: if the durable consumed-marker write is
    lost to a fault and the process then restarts, the boot-rebuilt registry
    re-derives ``notify_pending=True`` from the child metadata and the parent's next
    turn re-injects the SAME notification once more — the model sees a repeat and
    decides. Never silent loss."""

    reg = app.state.agent_task_registry
    task = reg.get(task_id)
    if task is None or not task.is_terminal:
        return None
    updated = reg.mark_consumed(task_id, datetime.now(timezone.utc).isoformat())
    if updated is None:
        # Already consumed by a concurrent/earlier caller (the atomic once-guard).
        return None
    # Durable so a boot-rebuilt registry does not re-inject an already-consumed
    # task (mirrors _persist_delegation_reported). Catch the FULL surface
    # persist_agent_task can raise — AgentTaskError (child gone) AND the OSError
    # family from the authoritative store's disk flush — with a structured reason,
    # never crash the turn (no-silent-fallback: the degrade is logged, and the
    # cross-crash at-least-once redelivery documented above is the honest recovery).
    try:
        persist_agent_task(app, updated)
    except (AgentTaskError, OSError) as exc:
        logger.warning(
            "agent_task consumed not persisted reason=%s task=%s err=%r",
            getattr(exc, "reason", type(exc).__name__),
            task_id,
            exc,
        )
    publish_agent_task_event(app, updated, AGENT_TASK_CONSUMED_EVENT)
    return updated


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
