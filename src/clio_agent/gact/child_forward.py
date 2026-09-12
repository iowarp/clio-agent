"""AgentTask lifecycle for a forwarded child HITL prompt (#1113 finding 5).

When an unattended child turn pauses for user input, ``turn_spawn._on_child_done``
forwards the pending question to the parent's HITL surface (see
:mod:`clio_agent.gact.elicitation_bridge`) instead of failing with the deleted
``child_requires_user_input`` reason. This module binds that forward's lifecycle to
the :class:`~clio_agent.gact.agent_tasks.AgentTask` so EVERY edge terminates typed
and frees the concurrency slot — never a hang:

* no pending question to forward -> :func:`fail_child_task` now;
* a headless parent that never answers -> :func:`arm_forward_deadline` fails it
  after a bounded, configurable deadline (default = the elicitation park window);
* a parent cancel / decline -> ``relay_forwarded_cancel`` -> :func:`fail_forwarded_child_task`.

Helpers that live in ``turn_spawn`` (``_fire_subagent_stop`` / ``_admit_next_queued``
/ ``_now``) are imported function-locally to keep the module-load graph acyclic.
"""

from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING, Any

from clio_agent.gact.agent_tasks import (
    AGENT_TASK_EVENTS,
    STATUS_COMPLETED,
    STATUS_FAILED,
    persist_agent_task,
    publish_agent_task_event,
)

if TYPE_CHECKING:
    from fastapi import FastAPI

logger = logging.getLogger(__name__)

__all__ = [
    "arm_forward_deadline",
    "fail_child_task",
    "fail_forwarded_child_task",
    "forward_waiting_child",
    "settle_or_attach_forwarded_task",
]


def forward_waiting_child(
    app: "FastAPI", task: Any, child_sess: Any, child_sid: str, mode: str
) -> bool:
    """HITL-in-child (#1113): forward a paused child's pending question to the parent.

    An unattended child cannot answer its own user question. If its turn paused
    (``waiting_user``), FORWARD the pending question to the parent's HITL surface
    instead of failing (replaces the deleted ``child_requires_user_input`` fail
    path). Every edge terminates typed, nothing hangs: no pending question to
    forward -> :func:`fail_child_task` now; forwarded -> the task stays in
    progress but :func:`arm_forward_deadline` bounds an unattended-parent deadline
    that terminates it typed and frees the slot; a parent answer resumes the child
    (the caller's completion hook runs again at true completion); a parent
    cancel/decline relays down to :func:`fail_forwarded_child_task`.

    Returns ``True`` when the child was waiting and this call handled it (the
    caller must return without its own terminal handling); ``False`` when the
    child was not paused for user input (the caller proceeds as normal).
    """

    from clio_agent.gact.elicitation_forwarding import (  # noqa: PLC0415
        forward_child_question_to_parent,
    )

    if child_sess is None or getattr(child_sess, "status", "") != "waiting_user":
        return False
    forwarded_qid = forward_child_question_to_parent(app, task, child_sid)
    if forwarded_qid is None:
        fail_child_task(app, task, child_sid, "child_question_forward_failed", mode)
    else:
        arm_forward_deadline(app, forwarded_qid)
    return True


def fail_child_task(app: "FastAPI", task: Any, child_sid: str, reason: str, mode: str) -> None:
    """Terminate a child task with a typed FAILED reason + full lifecycle side effects.

    Mirrors the completion path (persist, publish, SubagentStop, admit next queued)
    so a HITL edge that cannot proceed frees its concurrency slot instead of hanging.
    ``notify_pending`` follows the async-observe contract.
    """

    from clio_agent.gact.turn_spawn import (  # noqa: PLC0415
        _admit_next_queued,
        _now,
        finalize_child_task_terminal,
    )

    reg = app.state.agent_task_registry
    try:
        updated = reg.transition(
            task.task_id,
            STATUS_FAILED,
            error_reason=reason,
            notify_pending=(mode == "async"),
            updated_at=_now(),
        )
    except Exception:  # noqa: BLE001 - a transition race falls back to the current row
        updated = reg.get(task.task_id) or task
    persist_agent_task(app, updated)
    publish_agent_task_event(app, updated, AGENT_TASK_EVENTS[updated.status])
    finalize_child_task_terminal(app, updated, child_sid)  # #1305 review round F3
    _admit_next_queued(app)


def fail_forwarded_child_task(app: "FastAPI", task_id: str, reason: str) -> None:
    """Fail a task whose forwarded question was cancelled/declined/expired (bridge entry).

    ``elicitation_bridge.relay_forwarded_cancel`` calls this so a parent-side
    cancel/decline (or the unattended deadline) terminates the bound task typed and
    frees its slot. No-op when the task is already terminal.
    """

    task = app.state.agent_task_registry.get(task_id)
    if task is None or task.is_terminal:
        return
    fail_child_task(app, task, task.child_session_id, reason, "async")


def _forward_deadline_seconds(app: "FastAPI") -> float:
    """Bounded unattended-parent deadline (default matches the elicitation window)."""

    from clio_agent import conf  # noqa: PLC0415
    from clio_agent.gact.elicitation_bridge import DEFAULT_ELICITATION_TIMEOUT_S  # noqa: PLC0415

    configured = getattr(app.state, "child_forward_deadline_s", 0.0)
    if configured:
        return float(configured)
    return conf.resolve(
        "agents.child_forward_deadline_s",
        env="CLIO_CHILD_FORWARD_DEADLINE_S",
        default=DEFAULT_ELICITATION_TIMEOUT_S,
        cast=conf.as_float,
    )


def arm_forward_deadline(app: "FastAPI", forwarded_qid: str) -> None:
    """Arm a bounded backstop so an unanswered forwarded question cannot hang a task.

    After the deadline, if the forwarded question is still pending (headless parent),
    terminalize it, cancel the child question, and fail the task typed — freeing the
    concurrency slot. A parent answer/cancel before then makes this a no-op.
    """

    deadline = _forward_deadline_seconds(app)

    def _expire() -> None:
        from clio_agent.gact.elicitation_bridge import (  # noqa: PLC0415
            claim_question_transition,
        )
        from clio_agent.gact.elicitation_forwarding import (  # noqa: PLC0415
            relay_forwarded_cancel,
        )

        # Atomically claim expiry (first-wins vs a parent answer/cancel landing now).
        forwarded = claim_question_transition(app, forwarded_qid, "expired")
        if forwarded is None:
            return  # the parent answered/cancelled in time — nothing to do
        # Cancel the child question + fail the task with the TIMEOUT reason, freeing
        # the concurrency slot for a headless (never-answering) parent.
        relay_forwarded_cancel(app, forwarded, reason="child_forward_unattended_timeout")

    timer = threading.Timer(deadline, _expire)
    timer.daemon = True
    timer.start()


def settle_or_attach_forwarded_task(app: "FastAPI", task_id: str) -> None:
    """Bind a forwarded task to the outcome of applying its answer (#1113 finding 5 remnant).

    Called after a forwarded child answer is applied. If applying it launched a child
    turn (resume / plan-exit that resumes), attach :func:`_on_child_done` to that turn
    so the task settles at the turn's real completion. If NO turn launched (plan-exit
    ``exit_only`` — the question was answered and honored, the child went idle), the task
    would otherwise hang forever (its deadline is disabled once the parent question is
    answered): terminalize it SUCCESS and admit the next queued task, freeing the slot.
    """

    reg = app.state.agent_task_registry
    task = reg.get(task_id)
    if task is None or task.is_terminal:
        return
    child_sid = task.child_session_id

    from clio_agent.gact.turn_spawn import _on_child_done  # noqa: PLC0415

    in_flight = getattr(app.state, "in_flight_turns", {}).get(child_sid)
    if in_flight is not None:
        in_flight.add_done_callback(
            lambda _t, tid=task_id, csid=child_sid: _on_child_done(app, tid, csid, "async")
        )
        return
    _complete_forwarded_task(app, task)


def _complete_forwarded_task(app: "FastAPI", task: Any) -> None:
    """Terminalize a forwarded task SUCCESS (answered + honored, no turn) + admit next."""

    from clio_agent.gact.turn_spawn import (  # noqa: PLC0415
        _admit_next_queued,
        _now,
        finalize_child_task_terminal,
    )

    reg = app.state.agent_task_registry
    try:
        updated = reg.transition(
            task.task_id,
            STATUS_COMPLETED,
            result={
                "message_ref": "",
                "answer_excerpt": "forwarded user question answered",
                "workflow_state": {},
            },
            notify_pending=True,
            updated_at=_now(),
        )
    except Exception:  # noqa: BLE001 - a transition race falls back to the current row
        updated = reg.get(task.task_id) or task
    persist_agent_task(app, updated)
    publish_agent_task_event(app, updated, AGENT_TASK_EVENTS[updated.status])
    finalize_child_task_terminal(app, updated, task.child_session_id)  # #1305 review round F3
    _admit_next_queued(app)
