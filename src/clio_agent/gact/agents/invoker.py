"""The ``ExpertInvoker`` seam (#671, prepared by #948 S7 / #955).

Expert execution today is in-process by construction (``spawn_child_turn`` mints a
real child turn on a dedicated pool and projects it as an :class:`AgentTask`). #671
detangles that behind a **transport-abstracted boundary** so a later federation
campaign can swap the EXECUTOR — a detached loopback, then clio-core Context
Transport Primitives on a cluster — *behind this seam, not in front of every
caller*.

This module owns the boundary's **serializable** request / response / event shapes
and the in-process implementation:

* :data:`TaskSpec` — the spawn REQUEST. Re-exported verbatim from
  :mod:`clio_agent.gact.turn_spawn`: it is already a frozen, fully JSON-serializable
  dataclass (str/int fields + an optional plain-dict ``workflow_state``), so the
  seam reuses it rather than minting a duplicate twin (:func:`spec_to_wire` /
  :func:`spec_from_wire` prove the round-trip).
* :class:`TaskHandle` — what :meth:`ExpertInvoker.invoke` returns: the serializable
  identity a caller holds to later wait / check / cancel. A remote executor returns
  the same handle shape.
* :class:`TaskResult` — the serializable RESPONSE: a wire projection of an
  :class:`AgentTask` carrying the status lifecycle, the create/update timeline, the
  ``result`` payload (``message_ref`` / ``answer_excerpt`` / ``workflow_state``), the
  typed ``error_reason`` and a RESERVED ``artifact_ref``. It DROPS all six
  non-executor-boundary :class:`AgentTask` fields, in two classes: (a) parent-side
  observe-later + wire-dedup bookkeeping — ``notify_pending`` / ``consumed_at`` /
  ``delegation_reported`` — choreography that stays local under federation; and (b)
  the spawn-REQUEST / topology fields the parent already holds on the ``TaskSpec`` it
  authored — ``parent_turn_id`` / ``child_turn_id`` / ``fanout_bound`` — which the
  executor need not echo back in a result.
* :class:`TaskEvent` + :data:`TASK_EVENT_VOCABULARY` — the ``agent.task.*`` event
  family the substrate publishes, as a serializable boundary shape + the
  status→event-type mapping.

Relay compatibility (clio-relay ``docs/mcp-tasks.md``): relay's durable job
observations project onto the MCP task vocabulary without changing relay's stored
state words. :data:`RELAY_STATE_MAP` records that complete cross-repository table,
including the distinction between a completed tool error (``isError``) and a failed
protocol operation.

**The seam IS the same substrate.** :class:`InProcessExpertInvoker` delegates every
operation to the existing spawn / registry / cancel primitives
(``spawn_child_turn_threadsafe`` / ``AgentTaskRegistry`` / ``cancel_agent_task``) —
so introducing it creates **no second execution pathway** and no behavior change.
The model-facing spawn-runtime tools continue to call those primitives directly in
this slice (they layer local wire-rendering choreography — semantic events,
``expert_handoff`` Parts, observe-later consumption — that is parent-side, not
transport, and stays local under federation); when federation lands they migrate to
route the SUBSTRATE calls through an :class:`ExpertInvoker`, swapping
``InProcess`` for a detached implementation. The parity suite
(``tests/test_gact/test_invoker_s7.py``) proves the in-process invoker is
record-, event- and typed-error-identical to the direct substrate calls today.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any, Optional, Protocol, Sequence, runtime_checkable

from clio_agent.gact.agent_tasks import (
    AGENT_TASK_CONSUMED_EVENT,
    AGENT_TASK_EVENTS,
    STATUS_QUEUED,
    TERMINAL_STATUSES,
    AgentTask,
)

# Reuse — NOT duplicate — the request shape and the spawn/cancel primitives.
from clio_agent.gact.turn_spawn import (
    SpawnError,
    TaskSpec,
    cancel_agent_task,
    spawn_child_turn_threadsafe,
)

if TYPE_CHECKING:
    from fastapi import FastAPI

    from clio_agent.gact.events import Event

__all__ = [
    "TaskSpec",
    "SpawnError",
    "TaskHandle",
    "TaskResult",
    "TaskEvent",
    "InvokerError",
    "ExpertInvoker",
    "InProcessExpertInvoker",
    "TASK_EVENT_VOCABULARY",
    "TASK_CONSUMED_EVENT",
    "RELAY_STATE_MAP",
    "task_event_type",
    "spec_to_wire",
    "spec_from_wire",
]


# ---------------------------------------------------------------------------
# Event vocabulary (the agent.task.* family)
# ---------------------------------------------------------------------------

# The status → wire-event mapping the substrate publishes on every lifecycle edge
# (queued/started/completed/failed/cancelled). Re-exported from the record module so
# there is ONE catalog; the seam adds the terminal ``consumed`` marker event.
TASK_EVENT_VOCABULARY: dict[str, str] = dict(AGENT_TASK_EVENTS)
TASK_CONSUMED_EVENT: str = AGENT_TASK_CONSUMED_EVENT

# Relay observation → MCP task projection. Relay keeps its durable JobState words;
# adapters use this table at the protocol boundary. ``input_required`` is a durable
# outstanding-input observation, while ``tool-fail`` and ``protocol`` distinguish
# two outcomes of relay ``failed``: completed tool work with ``isError`` versus a
# protocol-level task failure.
RELAY_STATE_MAP: dict[str, dict[str, str | bool]] = {
    "queued": {"status": "working"},
    "leased": {"status": "working"},
    "running": {"status": "working"},
    "input_required": {"status": "input_required"},
    "succeeded": {"status": "completed", "isError": False},
    "tool-fail": {"status": "completed", "isError": True},
    "protocol": {"status": "failed"},
    "canceled": {"status": "cancelled"},
}


def task_event_type(status: str) -> str:
    """The ``agent.task.*`` wire-event type for a lifecycle ``status``.

    Args:
        status: A record status (``queued``/``running``/``completed``/``failed``/
            ``cancelled``).

    Returns:
        The event type string (e.g. ``"agent.task.completed"``).

    Raises:
        InvokerError: If ``status`` is not a known lifecycle status
            (reason ``unknown_status``).
    """

    try:
        return TASK_EVENT_VOCABULARY[status]
    except KeyError as exc:
        raise InvokerError(
            f"no agent.task.* event for status {status!r}", reason="unknown_status"
        ) from exc


# ---------------------------------------------------------------------------
# Serializable boundary shapes
# ---------------------------------------------------------------------------


class InvokerError(Exception):
    """A refused invoker operation, carrying a typed ``reason`` (no free-form wire
    strings). ``invoke`` propagates the substrate's :class:`SpawnError` unchanged
    (already typed); this covers the seam's own boundary errors (unknown handle,
    unknown status)."""

    def __init__(self, message: str, *, reason: str) -> None:
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True)
class TaskHandle:
    """The serializable identity :meth:`ExpertInvoker.invoke` returns.

    A caller holds it to later :meth:`ExpertInvoker.wait` / :meth:`~ExpertInvoker.check`
    / :meth:`~ExpertInvoker.cancel`. Serializable in AND out so a remote executor can
    return one and a parent can persist / relay it. ``status`` / ``queued_reason``
    are the point-in-time values at invoke (``running`` or ``queued`` at the cap) —
    a live poll goes through :meth:`~ExpertInvoker.check`.
    """

    task_id: str
    parent_session_id: str
    child_session_id: str
    status: str = STATUS_QUEUED
    queued_reason: str = ""
    run_index: int = 0
    depth: int = 1

    @classmethod
    def from_task(cls, task: AgentTask) -> "TaskHandle":
        """Project a handle from a freshly-spawned :class:`AgentTask`."""

        return cls(
            task_id=task.task_id,
            parent_session_id=task.parent_session_id,
            child_session_id=task.child_session_id,
            status=task.status,
            queued_reason=task.queued_reason,
            run_index=task.run_index,
            depth=task.depth,
        )

    def to_wire(self) -> dict[str, Any]:
        """The JSON-serializable dict form."""

        return asdict(self)

    @classmethod
    def from_wire(cls, data: dict[str, Any]) -> "TaskHandle":
        """Reconstruct a handle from its wire dict (tolerates unknown keys)."""

        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in data.items() if k in known})


@dataclass(frozen=True)
class TaskResult:
    """The serializable RESPONSE of a wait / check — a wire projection of an
    :class:`AgentTask`.

    Carries the status lifecycle, the create/update timeline, the ``result`` payload
    (``{message_ref, answer_excerpt, workflow_state}``), the typed ``error_reason``
    and a RESERVED ``artifact_ref`` (the #670 artifacts campaign fills it with a
    spill ref; carried from day one so a federation record matches).

    It OMITS all six :class:`AgentTask` fields that are not part of the executor
    boundary, in two classes:

    * parent-side observe-later + wire-dedup bookkeeping — ``notify_pending`` /
      ``consumed_at`` / ``delegation_reported`` — choreography that stays local under
      federation; and
    * spawn-REQUEST / topology fields the parent already holds on the ``TaskSpec`` it
      authored — ``parent_turn_id`` / ``child_turn_id`` / ``fanout_bound`` — which the
      executor need not echo back in its result.
    """

    task_id: str
    parent_session_id: str
    child_session_id: str
    agent_ref: dict[str, str] = field(default_factory=dict)
    depth: int = 1
    run_index: int = 0
    status: str = STATUS_QUEUED
    queued_reason: str = ""
    error_reason: str = ""
    created_at: str = ""
    updated_at: str = ""
    result: Optional[dict[str, Any]] = None
    # RESERVED — filled by the artifacts campaign (#670); present so federation
    # records match the durable relay ``ArtifactRef`` vocabulary from day one.
    artifact_ref: str = ""

    @property
    def is_terminal(self) -> bool:
        """Whether the task has reached a terminal status."""

        return self.status in TERMINAL_STATUSES

    @classmethod
    def from_task(cls, task: AgentTask) -> "TaskResult":
        """Project a result from an :class:`AgentTask` record."""

        return cls(
            task_id=task.task_id,
            parent_session_id=task.parent_session_id,
            child_session_id=task.child_session_id,
            agent_ref=dict(task.agent_ref),
            depth=task.depth,
            run_index=task.run_index,
            status=task.status,
            queued_reason=task.queued_reason,
            error_reason=task.error_reason,
            created_at=task.created_at,
            updated_at=task.updated_at,
            result=dict(task.result) if task.result is not None else None,
            artifact_ref=task.artifact_ref,
        )

    def to_wire(self) -> dict[str, Any]:
        """The JSON-serializable dict form."""

        return asdict(self)

    @classmethod
    def from_wire(cls, data: dict[str, Any]) -> "TaskResult":
        """Reconstruct a result from its wire dict (tolerates unknown keys)."""

        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in data.items() if k in known})


@dataclass(frozen=True)
class TaskEvent:
    """A serializable boundary event — one ``agent.task.*`` lifecycle edge.

    Mirrors what ``publish_agent_task_event`` puts on the bus (an ``asdict(task)``
    payload on both the parent and child channels), reshaped as the seam's transport
    event so a detached executor folds the same events back into the parent context
    (``arc/live.py`` already accepts raw producer events).
    """

    event_type: str
    task_id: str
    session_id: str
    status: str
    payload: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_bus_event(cls, event: "Event") -> "TaskEvent":
        """Project a :class:`TaskEvent` from a published bus :class:`Event`.

        Raises:
            InvokerError: If ``event`` is not an ``agent.task.*`` event
                (reason ``not_a_task_event``).
        """

        if not str(event.type).startswith("agent.task."):
            raise InvokerError(
                f"{event.type!r} is not an agent.task.* event", reason="not_a_task_event"
            )
        payload = dict(event.payload or {})
        return cls(
            event_type=str(event.type),
            task_id=str(payload.get("task_id", "")),
            session_id=str(event.session_id or ""),
            status=str(payload.get("status", "")),
            payload=payload,
        )

    def to_wire(self) -> dict[str, Any]:
        """The JSON-serializable dict form."""

        return asdict(self)


# ---------------------------------------------------------------------------
# TaskSpec (re)serialization helpers — proving the reused request shape round-trips
# ---------------------------------------------------------------------------


def spec_to_wire(spec: TaskSpec) -> dict[str, Any]:
    """Serialize a reused :class:`TaskSpec` to a JSON-safe dict (round-trip proof
    that the existing request shape needs no duplicate twin)."""

    return json.loads(json.dumps(asdict(spec)))


def spec_from_wire(data: dict[str, Any]) -> TaskSpec:
    """Reconstruct a :class:`TaskSpec` from its wire dict (tolerates unknown keys)."""

    known = set(TaskSpec.__dataclass_fields__)
    return TaskSpec(**{k: v for k, v in data.items() if k in known})


# ---------------------------------------------------------------------------
# The boundary
# ---------------------------------------------------------------------------


@runtime_checkable
class ExpertInvoker(Protocol):
    """The transport-abstracted expert-execution boundary (#671).

    Four operations carry the parent's decision to a child executor and the child's
    result/status back — all over serializable shapes. The boundary introduces **no
    clio-side routing/completion heuristic**: it spawns what it is told, reports what
    happened, and cancels on request; the parent (the model) decides everything else.

    Implementations:

    * :class:`InProcessExpertInvoker` — wraps today's in-process spawn substrate
      (this slice; parity-gated).
    * (later, #671) a detached loopback impl over a local socket, then clio-core
      Context Transport Primitives on a cluster — same boundary, swapped executor.
    """

    def invoke(self, spec: TaskSpec) -> TaskHandle:
        """Spawn ``spec``'s declared child expert; return its serializable handle
        (already ``running``, or ``queued`` at the concurrency cap). Raises
        :class:`SpawnError` (typed reason) for a refused spawn."""
        ...

    def wait(self, handle: TaskHandle, timeout_s: float) -> TaskResult:
        """Block up to ``timeout_s`` for the task to reach a terminal status, then
        return its :class:`TaskResult` (the current, possibly non-terminal record on
        timeout — the caller decides how to proceed)."""
        ...

    def check(self, handles: Sequence[TaskHandle]) -> list[TaskResult]:
        """Non-blocking poll: the current :class:`TaskResult` for each handle, in
        order."""
        ...

    def cancel(self, handle: TaskHandle) -> bool:
        """Cancel the task and cascade to its descendants; return whether anything
        was cancelled."""
        ...


class InProcessExpertInvoker:
    """In-process :class:`ExpertInvoker` — a thin, behavior-preserving seam over the
    existing spawn substrate.

    Every operation is a direct delegation to the primitive the spawn-runtime tools
    already use, so the invoker and the direct calls are the SAME execution pathway
    (proven record-, event- and error-identical by the S7 parity suite). No wire
    rendering, no observe-later consumption, no merge — those are parent-side
    concerns layered ABOVE the boundary by the tools, not transport, and stay local
    under federation.
    """

    def __init__(self, app: "FastAPI") -> None:
        self._app = app

    @property
    def app(self) -> "FastAPI":
        """The FastAPI app whose spawn substrate this invoker drives."""

        return self._app

    def invoke(self, spec: TaskSpec) -> TaskHandle:
        """Spawn ``spec``'s declared child via ``spawn_child_turn_threadsafe`` (the
        loop-safe substrate entry) and return its handle.

        Propagates :class:`SpawnError` unchanged for a refused spawn (undeclared
        child / depth cap) — the typed reason is the parity contract.
        """

        task = spawn_child_turn_threadsafe(self._app, spec)
        return TaskHandle.from_task(task)

    def wait(self, handle: TaskHandle, timeout_s: float) -> TaskResult:
        """Block on the task's completion Event (the S6 wait primitive) up to
        ``timeout_s`` and project the resulting record.

        Validates the handle FIRST: an unknown ``task_id`` raises
        :class:`InvokerError` (``unknown_task``) rather than blocking the full budget
        on a fresh never-set Event — the same footgun ``wait_agent_tasks`` guards.
        On timeout the current (non-terminal) record is returned; the caller decides.
        """

        reg = self._app.state.agent_task_registry
        task = reg.get(handle.task_id)
        if task is None:
            raise InvokerError(f"unknown task {handle.task_id!r}", reason="unknown_task")
        reg.event(handle.task_id).wait(timeout=max(0.0, float(timeout_s or 0.0)))
        task = reg.get(handle.task_id)
        if task is None:  # pragma: no cover - retained records are never removed
            raise InvokerError(f"task {handle.task_id!r} vanished mid-wait", reason="unknown_task")
        return TaskResult.from_task(task)

    def check(self, handles: Sequence[TaskHandle]) -> list[TaskResult]:
        """Non-blocking poll of each handle's current record.

        An unknown ``task_id`` raises :class:`InvokerError` (``unknown_task``): a
        handle not minted by this invoker is a caller error, surfaced typed rather
        than silently dropped from the batch (no-silent-fallback).
        """

        reg = self._app.state.agent_task_registry
        results: list[TaskResult] = []
        for handle in handles:
            task = reg.get(handle.task_id)
            if task is None:
                raise InvokerError(f"unknown task {handle.task_id!r}", reason="unknown_task")
            results.append(TaskResult.from_task(task))
        return results

    def cancel(self, handle: TaskHandle) -> bool:
        """Cancel the task + cascade to its descendants via ``cancel_agent_task``;
        return whether anything was cancelled (``False`` for an unknown / already
        terminal task with no live descendants — cancellation is idempotent)."""

        return cancel_agent_task(self._app, handle.task_id)
