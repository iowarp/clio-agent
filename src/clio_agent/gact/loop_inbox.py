"""Per-session loop inbox — the mid-turn wake carrier (#1035, epic #1031 Pillar 2).

A background child spawned in an EARLIER turn already surfaces on the parent's
NEXT turn via the ``notify_pending`` observe-later feed (``enrichment
.inject_pending_agent_task_notifications``). This module adds the complementary
*mid-turn* path: a child that finishes DURING the parent's turn drops a
data-light :class:`InboxEvent` into the parent session's :class:`LoopInbox`, and
the parent's next ReAct tool-observation boundary drains it — the completion is
appended to the model's observation string in the SAME turn instead of waiting
for the next one.

Design invariants (this slice, #1035):

* **Data-light events.** :class:`InboxEvent` carries only ``kind`` + ``task_id``;
  the human/model text is composed at drain time from the ``AgentTaskRegistry``
  via :func:`enrichment._notify_block`, so the ``AgentTask`` stays the single
  source of truth (no duplicated result payload on the inbox).
* **At-least-once, never at-most-once.** The inbox is a latency optimization, not
  a delivery guarantee: ``notify_pending`` stays set on the task, so if the mid-turn
  wake is dropped (bounded-overflow, no active turn, unknown session) the next-turn
  injection still delivers it. Overflow drops the OLDEST with a TYPED log reason.
* **No double-surface.** A completion drained mid-turn is marked consumed through
  the EXISTING once-gate (``agent_tasks.consume_notification`` →
  ``AgentTaskRegistry.mark_consumed``), so the next-turn injection skips it — and
  vice-versa (whichever consumer claims first wins, atomically under the registry
  lock).
* **Acyclic edge preserved.** ``tools.execution`` imports NO ``gact``; it reaches
  this drain only as an injected ``Callable`` on ``ToolRuntimeHooks`` (wired in
  ``app.py`` → ``runtime.app_state.resolve_tool_runtime``). This module is gact
  and may import gact freely.

#1036 (Producer B) adds a second event kind — ``user_message`` — the mid-turn
*steer*: a user POST that lands while a turn is already running is no longer a 409;
the message is persisted (marked ``mid_turn_steer``) and a data-carrying
:class:`InboxEvent` (``kind="user_message"``, its ``text`` + ``metadata``) is
enqueued. The running turn's next tool boundary drains it and surfaces a
``### steer`` grounding block (USER-authored, trusted, but off the model-output
lane) in the SAME turn. A steer that is never drained mid-turn (the turn ended
first) is re-driven by the idle hook into EXACTLY ONE new turn
(``drain_inbox_to_new_turn``). The ``deferred_resumes`` stash (an ask-user
answer that arrived while busy) is folded into this same carrier. De-dup is
inherent in the atomic pop-all :meth:`LoopInbox.drain` — a steer surfaces exactly
once (mid-turn OR idle, whichever drains first).

Out of scope here (deferred): the live handle (#1037).
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Literal

from clio_agent.gact.agent_tasks import STATUS_COMPLETED, STATUS_FAILED

if TYPE_CHECKING:
    from fastapi import FastAPI

logger = logging.getLogger(__name__)

# Bound on buffered wakes per session. A parent turn drains at every tool
# boundary, so this only needs to absorb a burst of children finishing between
# two boundaries; on overflow the OLDEST is dropped with a typed reason and the
# next-turn ``notify_pending`` fallback still carries it (never silent loss).
_INBOX_MAXLEN = 64

InboxKind = Literal["child_completed", "child_failed", "user_message"]

# Grounding-block header for a mid-turn user steer (#1036). Its OWN marker, NOT the
# task-notification marker: a steer is USER-authored (trusted) but still rides the
# server-grounding lane (surfaced in the model's tool-observation string), never the
# model-output lane, so it is unambiguously labelled as an out-of-band user message
# that arrived while the turn was running.
USER_STEER_MARKER = "## Mid-turn user steer (a message the user sent while this turn was running)"


def _now_iso() -> str:
    """UTC ISO-8601 stamp (matches the stamps used across agent_tasks)."""

    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class InboxEvent:
    """One data-light mid-turn wake.

    ``kind`` distinguishes a completed from a failed child (#1035) from a
    ``user_message`` steer (#1036). For the two child kinds ``task_id`` points
    back at the authoritative ``AgentTask`` from which the model-facing text is
    composed at drain time, and ``text``/``metadata`` are empty. For a
    ``user_message`` steer ``task_id`` is ``""``/meaningless and the event carries
    its own ``text`` (the user's steer message) plus ``metadata`` (e.g. the
    ask-user-resume bookkeeping when the fold re-drives an answer). ``enqueued_at``
    is a stamp for ordering/diagnostics only.
    """

    kind: InboxKind
    task_id: str
    enqueued_at: str = field(default_factory=_now_iso)
    text: str = ""
    metadata: dict = field(default_factory=dict)


class LoopInbox:
    """A bounded, thread-safe queue of :class:`InboxEvent` for ONE session.

    Writers are child done-callback threads (``turn_spawn._on_child_done`` →
    Producer A); the reader is the parent's tool-executor thread (the drain
    step-hook). One :class:`threading.RLock` guards a bounded
    ``deque(maxlen=_INBOX_MAXLEN)``; :meth:`put` and :meth:`drain` are the only
    mutators, so put-vs-drain across those threads is race-free.
    """

    def __init__(self, maxlen: int = _INBOX_MAXLEN) -> None:
        self._lock = threading.RLock()
        self._events: deque[InboxEvent] = deque(maxlen=maxlen)

    def put(self, event: InboxEvent) -> None:
        """Append ``event``; on overflow drop the OLDEST with a TYPED reason.

        A ``deque(maxlen=...)`` silently discards the leftmost element when full,
        which would be an unlogged degrade (a no-silent-fallback violation). We
        detect the full condition first and emit a structured reason naming the
        dropped task; the dropped completion still has its next-turn
        ``notify_pending`` fallback, so this is latency, never loss.
        """

        with self._lock:
            if len(self._events) == self._events.maxlen:
                dropped = self._events[0]
                logger.warning(
                    "loop_inbox overflow reason=inbox_full dropped_task=%s dropped_kind=%s "
                    "maxlen=%s (next-turn notify_pending fallback still delivers it)",
                    dropped.task_id,
                    dropped.kind,
                    self._events.maxlen,
                )
            self._events.append(event)

    def drain(self) -> list[InboxEvent]:
        """Pop-all snapshot under the lock: return every buffered event and clear."""

        with self._lock:
            events = list(self._events)
            self._events.clear()
            return events

    def peek_nonempty(self) -> bool:
        """True iff at least one event is buffered (cheap, does not consume)."""

        with self._lock:
            return len(self._events) > 0


def inbox_for(app: "FastAPI", session_id: str) -> LoopInbox:
    """Get-or-create the :class:`LoopInbox` for ``session_id`` on ``app.state``.

    ``dict.setdefault`` is atomic under the GIL, so two threads racing the first
    put for a fresh session still share exactly one inbox.
    """

    inboxes: dict[str, LoopInbox] = app.state.loop_inboxes
    existing = inboxes.get(session_id)
    if existing is not None:
        return existing
    return inboxes.setdefault(session_id, LoopInbox())


def enqueue_completion_wake(app: "FastAPI", task: object) -> None:
    """Producer A: wake the parent MID-TURN when a child finishes during its turn.

    Called from ``turn_spawn._on_child_done`` after the terminal
    ``publish_agent_task_event``. Enqueues a completion :class:`InboxEvent` on the
    parent's inbox ONLY when the parent session currently has a turn in flight
    (``turn_runner.busy``) — the mid-turn wake is pointless (and would leak past
    the turn) otherwise; the next-turn ``notify_pending`` injection covers the
    idle-parent case. Terminal but non-notify states (a CANCELLED child) are
    skipped. This runs on the child's done-callback thread and MUST NEVER raise
    into it, so every failure is caught and logged with a typed reason.
    """

    try:
        status = getattr(task, "status", "")
        if status not in (STATUS_COMPLETED, STATUS_FAILED):
            return
        task_id = getattr(task, "task_id", "")
        # Re-derive the parent from the authoritative registry (the task passed in
        # is a snapshot; the registry is the truth).
        reg = app.state.agent_task_registry
        current = reg.get(task_id)
        parent_sid = getattr(current, "parent_session_id", "") if current is not None else ""
        if not parent_sid:
            return
        runner = getattr(app.state, "turn_runner", None)
        if runner is None or not runner.busy(parent_sid):
            return
        kind: InboxKind = "child_completed" if status == STATUS_COMPLETED else "child_failed"
        inbox_for(app, parent_sid).put(InboxEvent(kind=kind, task_id=task_id))
    except Exception as exc:  # noqa: BLE001 - a wake must never break child completion
        logger.warning(
            "loop_inbox wake failed reason=wake_enqueue_error task=%s err=%r",
            getattr(task, "task_id", "?"),
            exc,
        )


def enqueue_user_steer(
    app: "FastAPI",
    session_id: str,
    text: str,
    metadata: dict | None = None,
) -> None:
    """Producer B: enqueue a mid-turn user *steer* onto ``session_id``'s inbox.

    Sibling of :func:`enqueue_completion_wake`. Used by both Producer B callers —
    the POST /messages busy branch (a user message that arrived while a turn runs)
    and the ask-user resume fold (an answer that arrived while busy). The event
    carries the user's ``text`` + ``metadata``; the running turn's next tool
    boundary drains it into a ``### steer`` grounding block, or — if the turn ends
    first — the idle hook re-drives residual steers into ONE new turn. This does
    NOT start a turn and does NOT persist a message (the POST route persists the
    ``mid_turn_steer`` message; the idle re-drive persists via a new turn).
    """

    inbox_for(app, session_id).put(
        InboxEvent(
            kind="user_message",
            task_id="",
            text=text,
            metadata=dict(metadata or {}),
        )
    )


def drain_inbox_to_new_turn(app: "FastAPI", sid: str) -> None:
    """Re-drive residual mid-turn user *steers* into EXACTLY ONE new turn (#1036).

    The turn-runner idle-hook body (installed in ``app.py``), fired when ``sid``'s
    turn slot clears. A steer (a mid-turn POST, or a folded ask-user resume) that
    the running turn never drained is still buffered here; we drain and start ONE
    new turn for all residual steers so the user's message is never dropped. Non-
    steer events (Producer A child wakes enqueued at the boundary) are put back —
    they are not turn-drivers and their next-turn ``notify_pending`` fallback still
    delivers them. If the session is gone / agent unavailable / a turn re-acquired
    the slot, every event stays buffered for the next idle transition.
    """

    inbox = app.state.loop_inboxes.get(sid)
    if inbox is None or not inbox.peek_nonempty():
        return
    sess = app.state.sessions.get(sid)
    if sess is None:
        return  # session gone; nothing to resume into
    if app.state.agent is None or app.state.turn_runner.busy(sid):
        return  # not ready — leave buffered for the next idle transition
    events = inbox.drain()
    steers = [e for e in events if e.kind == "user_message"]
    for residual in events:
        if residual.kind != "user_message":
            inbox_for(app, sid).put(residual)
    if not steers:
        return

    from clio_agent.gact.events import Event  # noqa: PLC0415
    from clio_agent.gact.turn import _start_background_user_turn  # noqa: PLC0415

    # One turn total: concatenate residual steer texts in arrival order; merge their
    # metadata (later wins) so a folded ask-user resume carries its ask_user_* keys.
    metadata: dict = {}
    for e in steers:
        metadata.update(e.metadata or {})
    resumed_msg = _start_background_user_turn(
        app,
        sid,
        sess,
        "\n\n".join(e.text for e in steers if e.text),
        metadata=metadata,
        prev_status=str(getattr(sess, "status", "idle") or "idle"),
    )
    # A folded ask-user resume still publishes user_question.resumed so the answer's
    # delivery is observable on the trace/API exactly as the live path emits it.
    for e in steers:
        if e.metadata.get("ask_user_resume"):
            app.state.bus.publish(
                Event(
                    type="user_question.resumed",
                    session_id=sid,
                    payload={
                        "question_id": e.metadata.get("question_id", ""),
                        "session_id": sid,
                        "queued_user_message_id": resumed_msg.id,
                        "deferred": True,
                    },
                )
            )


def drain_active_session_inbox(app: "FastAPI") -> str:
    """Drain the ACTIVE session's inbox and return a composed model-facing block.

    The injected drain callable's body. Resolves the active session (the parent
    whose turn is running the tool call), drains its inbox, and for each event
    composes ONE block via :func:`enrichment._notify_block` — marking that
    completion consumed through the EXISTING once-gate
    (:func:`agent_tasks.consume_notification`) so a mid-turn drain and the
    next-turn ``inject_pending_agent_task_notifications`` never double-surface the
    same task. Publishes ONE lightweight progress event on the PARENT session so
    the no-progress watchdog counts the drain as liveness. Returns the composed
    block, or ``""`` when there is nothing to surface.

    Self-guarding: this is called from the tool-executor hot path (via the
    injected ``Callable``), so any failure is caught and logged with a typed
    reason and returns ``""`` — a drain hiccup never breaks a tool call.
    """

    try:
        from clio_agent.gact import context as _ctx  # noqa: PLC0415

        sid = _ctx.active_session_id().strip()
        if not sid:
            return ""
        inbox = app.state.loop_inboxes.get(sid)
        if inbox is None:
            return ""
        events = inbox.drain()
        if not events:
            return ""

        # A user_message steer (#1036) is NOT a task: it MUST skip the once-gate
        # (consume_notification) and the delegation terminal entirely and surface a
        # ``### steer`` grounding block composed from its OWN text. The task path
        # below runs only for the child-completion kinds.
        steer_blocks: list[str] = []
        task_events = []
        for event in events:
            if event.kind == "user_message":
                steer_text = (event.text or "").strip()
                if steer_text:
                    steer_blocks.append(USER_STEER_MARKER + "\n\n" + steer_text)
                continue
            task_events.append(event)

        task_section = ""
        if task_events:
            from clio_agent.gact.agent_tasks import consume_notification  # noqa: PLC0415
            from clio_agent.gact.agents.resolution import (  # noqa: PLC0415
                _runtime_active_agent_blueprint_id,
            )
            from clio_agent.gact.agents.spawn_runtime import (  # noqa: PLC0415
                _emit_delegation_terminal,
            )
            from clio_agent.gact.enrichment import (  # noqa: PLC0415
                PENDING_TASK_NOTIFICATION_MARKER,
                _notify_block,
            )
            from clio_agent.gact.types import AgentDef  # noqa: PLC0415

            reg = app.state.agent_task_registry
            blueprint_id = _runtime_active_agent_blueprint_id(app, sid) or ""
            task_blocks: list[str] = []
            for event in task_events:
                task = reg.get(event.task_id)
                if task is None:
                    logger.warning(
                        "loop_inbox drain skip reason=unknown_task task=%s", event.task_id
                    )
                    continue
                # Claim the observe-later notification atomically. If a concurrent
                # wait/check (or an earlier drain) already consumed it, mark_consumed
                # returns None and we skip surfacing it — no double-surface.
                claimed = consume_notification(app, event.task_id)
                if claimed is None:
                    continue
                task_blocks.append(_notify_block(claimed))
                # Pair the consume with the delegation TERMINAL — the SAME choreography the
                # next-turn commit (consume_pending_agent_task_notifications) and wait/check
                # emit: blueprint.delegation.completed|failed + the return expert_handoff Part
                # + parent_resumed. Separately once-gated via delegation_reported, so a later
                # wait / next-turn inject reaching this task never double-emits. WITHOUT this a
                # mid-turn-drained fire-and-forget child would dangle (started, no terminal) and
                # render perpetually in-progress — the exact regression the observe-later commit
                # path exists to prevent. (A post-drain turn abort keeps the wire correct via
                # this terminal + the durable agent.task.completed; the parent re-observes via
                # the registry, so the completion is never lost — only the mid-turn auto-inject.)
                parent_id = task.agent_ref.get("requesting_expert_id", "") or "main"
                parent_def = AgentDef(
                    id=parent_id,
                    title=parent_id,
                    metadata={"agent_blueprint_id": blueprint_id},
                )
                _emit_delegation_terminal(app, sid, parent_def, task)
            if task_blocks:
                task_section = PENDING_TASK_NOTIFICATION_MARKER + "\n\n" + "\n\n".join(task_blocks)

        # Watchdog liveness: publish on the PARENT session even if every event was
        # already consumed elsewhere (the drain still did work this turn).
        surfaced = len(steer_blocks) + (1 if task_section else 0)
        _publish_drain_progress(app, sid, len(events), surfaced)

        # Steers first (the user's most recent intent leads), then task results.
        sections = [*steer_blocks, task_section]
        return "\n\n".join(s for s in sections if s)
    except Exception as exc:  # noqa: BLE001 - a drain must never break a tool call
        logger.warning("loop_inbox drain failed reason=drain_error err=%r", exc)
        return ""


def _publish_drain_progress(app: "FastAPI", sid: str, drained: int, surfaced: int) -> None:
    """Publish one non-transient progress event on the parent session (liveness)."""

    try:
        from clio_agent.gact.events import Event  # noqa: PLC0415

        app.state.bus.publish(
            Event(
                type="loop_inbox.drained",
                session_id=sid,
                payload={"drained": drained, "surfaced": surfaced},
            )
        )
    except Exception as exc:  # noqa: BLE001 - liveness telemetry is best-effort
        logger.warning("loop_inbox progress publish failed reason=publish_error err=%r", exc)


def _make_loop_inbox_drain(app: "FastAPI"):
    """Build the injected drain callable (mirrors ``_make_cancellation_checker``).

    Closes over ``app`` and returns a zero-arg callable that drains the active
    session's inbox; installed on ``app.state.pending_loop_inbox_drain`` and read
    by ``runtime.app_state.resolve_tool_runtime`` into ``ToolRuntimeHooks``.
    """

    def drain() -> str:
        return drain_active_session_inbox(app)

    return drain
