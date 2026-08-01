"""Human-facing LIVE execution handle for a spawned agent-task (#1037, epic #1031
Pillar 2 slice 3/3 — CLOSES Pillar 2).

Pillars 1+2 gave the parent MODEL the async menu (spawn / observe / wait / cancel /
mid-turn steer). This slice gives a HUMAN the same live view of ONE running child:
a single read-only projection (:func:`project_live_handle`) plus a steer producer
(:func:`enqueue_steer_or_raise`) reusing #1036's carrier against the child session.

Design invariants (RULE 4 — NO new store):

* **Pure read-only assembly.** :func:`project_live_handle` calls ONLY
  ``registry.get`` + ``app.state.messages.get`` + ``bus.session_events_since`` — all
  reads over stores that already exist. It NEVER touches a mutator
  (``persist_agent_task`` / ``registry.transition`` / ``consume_notification`` /
  ``publish_agent_task_event`` / ``_notify_block``): every one of those flips durable
  state (``notify_pending`` in particular), and a projection that observes a task
  must never consume/settle it. The handle re-sources cleanly on every call.
* **Handoff attribution by ``run_index``.** The ``expert_handoff`` Parts are NOT
  task-keyed; they live on the PARENT transcript. An ensemble spawns the SAME expert
  N times in one turn, so a child_agent match alone is ambiguous — the
  ``(child_agent, run_index)`` pair is the ensemble disambiguator.
* **Trust boundary on the child head.** The child's recent messages are
  MODEL-authored text a human will read; they are bounded (last N) + a truncated
  flag + an explicit label, and never presented as user/parent-authoritative.
* **Steer is never silently stranded.** A settled/gone/idle child's inbox is NEVER
  drained again (nothing re-fires its idle hook), so buffering a steer for it would
  strand it forever. :func:`enqueue_steer_or_raise` refuses with a typed 409
  ``child_not_running`` (carrying the specific sub-cause) unless the child has a
  genuinely running turn; only then does it reuse
  :func:`loop_inbox.enqueue_user_steer` against the child session.

clio-relay parity is a SHAPE obligation only: :class:`LiveTaskHandle` mirrors the
durable job-record vocabulary already on :class:`~clio_agent.gact.agent_tasks
.AgentTask` (status/timeline/artifact), so federation later swaps the executor
behind the seam — it introduces NO new store.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any, Optional

from fastapi import HTTPException

from clio_agent.gact.agent_tasks import AgentTask
from clio_agent.gact.loop_inbox import enqueue_user_steer
from clio_agent.gact.types import ErrorEnvelope, ErrorInfo

if TYPE_CHECKING:
    from fastapi import FastAPI

# Bound on the child-head window (trust boundary): the last N child messages a
# human reads, never the whole transcript. Older messages are elided with a
# ``truncated`` flag (never a silent drop).
_CHILD_HEAD_MAX = 10

# Per-part text cap in the child head: bounding the message COUNT is not enough — a
# single child message could carry an arbitrarily large text part, so each part's text
# is truncated to this many chars (with an explicit ``text_truncated`` flag).
_CHILD_HEAD_PART_TEXT_MAX = 4000

# The explicit trust-boundary label carried on the child head: this text is
# MODEL-authored (the child agent's output), so a human/consumer must not treat it
# as user- or parent-authoritative.
_CHILD_HEAD_LABEL = (
    "child-agent output (model-authored; a human reads it) — NOT user- or parent-authoritative text"
)

# Prefix the timeline filters to: the per-child lifecycle feed already fanned to
# the child session channel by ``publish_agent_task_event``.
_TIMELINE_PREFIX = "agent.task."

# Typed sub-causes for a rejected steer (all roll up to the 409 ``child_not_running``).
STEER_REJECT_TASK_TERMINAL = "task_terminal"
STEER_REJECT_CHILD_GONE = "child_session_gone"
STEER_REJECT_CHILD_IDLE = "child_idle"


@dataclass(frozen=True)
class LiveTaskHandle:
    """A human-facing live view of one spawned child task — pure projection.

    Mirrors the durable job-record vocabulary (clio-relay shape parity) assembled
    from stores that already exist; carries NO new persistent state.
    """

    task: dict[str, Any]
    """``asdict`` of the authoritative :class:`AgentTask` record."""

    timeline: list[dict[str, Any]] = field(default_factory=list)
    """The child channel's ``agent.task.*`` lifecycle events, oldest first."""

    handoff_parts: list[dict[str, Any]] = field(default_factory=list)
    """The PARENT-transcript ``expert_handoff`` Parts for THIS run (by run_index)."""

    child_head: dict[str, Any] = field(default_factory=dict)
    """Bounded, LABELED window over the child's recent (model-authored) messages."""

    actions: list[dict[str, Any]] = field(default_factory=list)
    """The async menu as human actions: observe / wait / cancel / steer."""


def _timeline_for(app: "FastAPI", child_session_id: str) -> list[dict[str, Any]]:
    """Project the child channel's ``agent.task.*`` lifecycle events (pure read).

    Reads the SAME bounded per-session replay history the SSE feed serves via
    ``bus.session_events_since`` — no new channel, no mutation.
    """

    if not child_session_id:
        return []
    events = app.state.bus.session_events_since(child_session_id)
    return [
        {
            "id": ev.id,
            "type": ev.type,
            "occurred_at": ev.occurred_at,
            "payload": ev.payload,
        }
        for ev in events
        if ev.type.startswith(_TIMELINE_PREFIX)
    ]


def _handoff_parts_for(app: "FastAPI", task: AgentTask) -> list[dict[str, Any]]:
    """The PARENT-transcript ``expert_handoff`` Parts attributable to THIS run.

    The Parts are NOT task-keyed (spawn_runtime appends them to the parent
    transcript keyed by the delegation lane). ``run_index`` resets per parent turn
    (``0,1,2`` in spawn order per ``(parent_turn_id, child expert)``), so
    ``(child_agent, run_index)`` alone COLLIDES across turns — a later turn that
    re-delegates to the same expert with the same ``run_index`` would collect this
    turn's siblings. The attribution key is therefore
    ``(child_agent, parent_turn_id, run_index)``: the enclosing parent-transcript
    ``Message.turn_id`` scopes to THIS task's spawning turn, ``run_index`` picks the
    right ensemble member within it. Pure read of
    ``app.state.messages[parent_session_id]``.

    Note: the STARTED handoff Part lives in the spawning turn (``parent_turn_id``); a
    RETURN Part emitted in a LATER turn (a fire-and-forget terminal firing after the
    parent moved on) is intentionally NOT collected here — its data is on the
    ``agent.task.*`` timeline + the record — because attributing it turn-blind would
    reintroduce the sibling collision. For a RUNNING task (the handle's primary use)
    there is no return Part yet, so this is exact.
    """

    expert_id = task.agent_ref.get("expert_id", "")
    out: list[dict[str, Any]] = []
    for message in app.state.messages.get(task.parent_session_id, []):
        if getattr(message, "turn_id", "") != task.parent_turn_id:
            continue
        for part in getattr(message, "parts", []):
            if getattr(part, "type", "") != "expert_handoff":
                continue
            if getattr(part, "child_agent", "") != expert_id:
                continue
            metadata = getattr(part, "metadata", None) or {}
            if metadata.get("run_index") != task.run_index:
                continue
            out.append(part.to_wire())
    return out


def _child_head(app: "FastAPI", child_session_id: str) -> dict[str, Any]:
    """A bounded, LABELED window over the child's recent messages (trust boundary).

    Returns the last :data:`_CHILD_HEAD_MAX` child messages via ``to_wire()`` with a
    ``truncated`` flag when older messages were elided, and an explicit label marking
    the content as model-authored child output (never user/parent-authoritative). A
    gone child yields an empty (but still labeled) window — tolerated gracefully.
    """

    messages = app.state.messages.get(child_session_id, [])
    total = len(messages)
    window = messages[-_CHILD_HEAD_MAX:] if total > _CHILD_HEAD_MAX else list(messages)
    return {
        "label": _CHILD_HEAD_LABEL,
        "session_id": child_session_id,
        "total": total,
        "returned": len(window),
        "truncated": total > len(window),
        "messages": [_bound_message_text(m.to_wire()) for m in window],
    }


def _bound_message_text(wire: dict[str, Any]) -> dict[str, Any]:
    """Cap oversized text parts in a child-head message (trust boundary).

    Bounding the message COUNT is not enough — a single child message could carry an
    arbitrarily large text part. Each part's ``text`` is truncated to
    :data:`_CHILD_HEAD_PART_TEXT_MAX` chars with an explicit ``text_truncated`` flag
    (never a silent drop). A pure transform of the wire dict; other fields pass through.
    """

    parts = wire.get("parts")
    if not isinstance(parts, list):
        return wire
    capped: list[Any] = []
    for part in parts:
        text = part.get("text") if isinstance(part, dict) else None
        if isinstance(text, str) and len(text) > _CHILD_HEAD_PART_TEXT_MAX:
            part = {
                **part,
                "text": text[:_CHILD_HEAD_PART_TEXT_MAX] + "…[truncated]",
                "text_truncated": True,
            }
        capped.append(part)
    return {**wire, "parts": capped}


def _live_actions(task_id: str, child_session_id: str) -> list[dict[str, Any]]:
    """The async menu rendered as human actions (the 4 modes).

    observe = re-read this ``/live`` projection; wait = subscribe the EXISTING
    per-session SSE bus for the child; cancel = the existing cancel route; steer =
    the new steer route. Descriptive only — the handle names the surfaces, it does
    not invoke them.
    """

    return [
        {
            "mode": "observe",
            "method": "GET",
            "path": f"/v1/agent-tasks/{task_id}/live",
            "description": "Re-read this live handle (poll the child's current state).",
        },
        {
            "mode": "wait",
            "method": "GET",
            "path": f"/v1/sessions/{child_session_id}/events",
            "description": "Subscribe the child session's SSE stream for live lifecycle/progress.",
        },
        {
            "mode": "cancel",
            "method": "POST",
            "path": f"/v1/agent-tasks/{task_id}/cancel",
            "description": "Cancel the child's in-flight turn (typed cancelled transition).",
        },
        {
            "mode": "steer",
            "method": "POST",
            "path": f"/v1/agent-tasks/{task_id}/steer",
            "description": (
                "Message the child using its retained placement; completed children wake."
            ),
        },
    ]


def project_live_handle(app: "FastAPI", task_id: str) -> Optional[LiveTaskHandle]:
    """Assemble a :class:`LiveTaskHandle` for ``task_id`` — PURE read-only.

    Returns ``None`` when the task is unknown (the route maps that to its typed
    ``not_found``). Calls ONLY ``registry.get`` + ``app.state.messages.get`` +
    ``bus.session_events_since`` — it mutates NOTHING (task metadata, notify_pending,
    the registry, and the bus history are all identical before and after). Tolerates
    a gone child gracefully: empty head + empty timeline, but still returns the
    authoritative record.
    """

    task = app.state.agent_task_registry.get(task_id)
    if task is None:
        return None
    return LiveTaskHandle(
        task=asdict(task),
        timeline=_timeline_for(app, task.child_session_id),
        handoff_parts=_handoff_parts_for(app, task),
        child_head=_child_head(app, task.child_session_id),
        actions=_live_actions(task_id, task.child_session_id),
    )


def _child_not_running_reason(app: "FastAPI", task: AgentTask) -> Optional[str]:
    """Return a typed sub-cause when the child cannot accept a steer, else ``None``.

    A settled agent-task child's inbox is NEVER drained again (nothing re-fires its
    idle hook), so a steer buffered for it would be stranded forever. The three
    non-running conditions — terminal task, gone child session, or a child with no
    in-flight turn — each yield a typed reason so the caller refuses instead of
    silently buffering.
    """

    if task.is_terminal:
        return STEER_REJECT_TASK_TERMINAL
    if app.state.sessions.get(task.child_session_id) is None:
        return STEER_REJECT_CHILD_GONE
    runner = getattr(app.state, "turn_runner", None)
    if runner is None or not runner.busy(task.child_session_id):
        return STEER_REJECT_CHILD_IDLE
    return None


def enqueue_steer_or_raise(
    app: "FastAPI", task: AgentTask, text: str, metadata: Optional[dict[str, Any]]
) -> None:
    """Enqueue a human steer onto the CHILD's inbox, or raise a typed 409.

    Refuses with a 409 ``child_not_running`` (carrying the specific sub-cause in
    ``details.reason``) when the child is terminal / gone / idle — a steer that could
    never be drained is a stranded message, never a silent buffer. Only a genuinely
    running child reuses #1036's producer (:func:`enqueue_user_steer`) against the
    CHILD session; the child's next tool boundary drains it into a ``### steer``
    grounding block.
    """

    if not (text or "").strip():
        raise HTTPException(
            status_code=422,
            detail=ErrorEnvelope(
                error=ErrorInfo(
                    error="invalid_request",
                    message="steer text must be non-empty",
                    details={"field": "text"},
                    recoverable=True,
                )
            ).model_dump(exclude_none=True),
        )
    reason = _child_not_running_reason(app, task)
    if reason is not None:
        raise HTTPException(
            status_code=409,
            detail=ErrorEnvelope(
                error=ErrorInfo(
                    error="child_not_running",
                    message=(
                        f"task {task.task_id!r} has no running child turn to steer "
                        f"(reason={reason}); a steer would be stranded"
                    ),
                    details={
                        "task_id": task.task_id,
                        "child_session_id": task.child_session_id,
                        "status": task.status,
                        "reason": reason,
                    },
                    recoverable=True,
                )
            ).model_dump(exclude_none=True),
        )
    enqueue_user_steer(app, task.child_session_id, text, metadata)
