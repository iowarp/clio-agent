"""Forwarding a paused child's question to the attended parent, and back.

Split out of :mod:`clio_agent.gact.elicitation_bridge` (no-accretion; that module
had reached the 800-line cap). The bridge owns the elicitation protocol itself --
schema translation, the parked future, the one atomic status transition. THIS
module owns the parent-forward: mirroring an unattended child's question onto the
root attended session, relaying a cancel down to the child and its task, and
delivering the parent's answer back through the owner path.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from clio_agent.gact.elicitation_bridge import (
    FORWARDED_QUESTION_SOURCE,
    _new_question,
    _pending_question_for,
    _publish_question_created,
    _record_reason,
    claim_question_transition,
    resolve_answered_question,
    resolve_elicitation,
)
from clio_agent.gact.permission_delivery import attended_session_id
from clio_agent.gact.types import UserQuestion

__all__ = [
    "deliver_forwarded_answer",
    "forward_child_question_to_parent",
    "relay_forwarded_cancel",
    "resolve_cancelled_question",
]


def forward_child_question_to_parent(app: Any, task: Any, child_sid: str) -> str | None:
    """Forward a paused child's pending question to the parent's HITL surface.

    Mirrors an unattended child's pending question onto the parent's (root attended)
    session, linked back so :func:`deliver_forwarded_answer` / :func:`relay_forwarded_cancel`
    relay the resolution. Returns the forwarded question id, or ``None`` (typed reason)
    when the child had none — the caller then terminates the task typed (replaces the
    deleted ``child_requires_user_input`` fail path).
    """

    child_q = _pending_question_for(app, child_sid)
    if child_q is None:
        _record_reason("child_waiting_without_question", child=child_sid)
        return None
    attended = attended_session_id(app, getattr(task, "parent_session_id", "") or child_sid)
    child_elicitation = child_q.metadata.get("elicitation")
    child_elicitation = child_elicitation if isinstance(child_elicitation, Mapping) else {}
    forwarded = _new_question(
        attended,
        prompt=child_q.prompt,
        kind=child_q.kind,
        options=list(child_q.options),
        source=FORWARDED_QUESTION_SOURCE,
        metadata={
            "forwarded_from_session": child_sid,
            "forwarded_from_question": child_q.id,
            "task_id": getattr(task, "task_id", ""),
            "invocation_id": child_q.metadata.get("invocation_id", "")
            or child_elicitation.get("invocation_id", ""),
        },
        owner_session_id=child_sid,
        attended_session_id=attended,
    )
    _publish_question_created(app, forwarded)
    bus = getattr(app.state, "bus", None)
    if bus is not None:
        from clio_agent.gact.events import Event  # noqa: PLC0415

        bus.publish(
            Event(
                type="user_question.forwarded",
                session_id=attended,
                payload={
                    "question_id": forwarded.id,
                    "forwarded_from_session": child_sid,
                    "forwarded_from_question": child_q.id,
                    "task_id": getattr(task, "task_id", ""),
                },
            )
        )
    return forwarded.id


def relay_forwarded_cancel(
    app: Any, forwarded: UserQuestion, *, reason: str = "child_forward_declined"
) -> bool:
    """Relay a parent-side cancel of a forwarded question down to the child + task.

    Cancels the mirrored child question and fails the bound AgentTask with a typed
    ``reason``, so a declined/cancelled/expired forward never leaves the child waiting
    or its slot pinned. Returns ``True`` when the row was a forwarded mirror.
    """

    child_qid = str(forwarded.metadata.get("forwarded_from_question") or "")
    task_id = str(forwarded.metadata.get("task_id") or "")
    if not child_qid and not task_id:
        return False
    if child_qid:
        claim_question_transition(app, child_qid, "cancelled")  # atomic, no-op if resolved
    if task_id:
        from clio_agent.gact.child_forward import fail_forwarded_child_task  # noqa: PLC0415

        fail_forwarded_child_task(app, task_id, reason)
    return True


def resolve_cancelled_question(app: Any, question: UserQuestion) -> bool:
    """Resolve a cancelled question that is an elicitation or a forwarded mirror.

    Shared cancel route: an in-flight elicitation wakes its parked call (typed cancel);
    a forwarded mirror relays the cancel to the child + fails the task. Returns ``True``
    when handled (route skips the idle transition), ``False`` for an ordinary ask.
    """

    if resolve_elicitation(app, question):
        return True
    if (
        question.metadata.get("forwarded_from_question")
        or question.source == FORWARDED_QUESTION_SOURCE
    ):
        return relay_forwarded_cancel(app, question)
    return False


def deliver_forwarded_answer(app: Any, deps: Any, forwarded: UserQuestion) -> None:
    """Relay a forwarded parent answer to the child question via the OWNER path.

    Applies the answer atomically then dispatches through the SAME
    :func:`resolve_answered_question` the route uses (plan-exit -> mode switch,
    elicitation -> wake parked call, ordinary ask -> resume). The task is then bound to
    the outcome via :func:`~clio_agent.gact.child_forward.settle_or_attach_forwarded_task`
    (turn -> settle at its completion; no turn / exit_only -> SUCCESS terminal + admit).
    Every unresumable edge terminalizes the task typed — the slot is never leaked (finding 5).
    """

    child_sid = str(forwarded.metadata.get("forwarded_from_session") or "")
    child_qid = str(forwarded.metadata.get("forwarded_from_question") or "")
    task_id = str(forwarded.metadata.get("task_id") or "")

    def _terminate(reason: str) -> None:
        _record_reason(reason, child=child_sid, question=child_qid)
        if task_id:
            from clio_agent.gact.child_forward import fail_forwarded_child_task  # noqa: PLC0415

            fail_forwarded_child_task(app, task_id, "child_forward_not_resumable")

    def _settle_or_attach(tid: str) -> None:
        if tid:
            from clio_agent.gact.child_forward import (
                settle_or_attach_forwarded_task,  # noqa: PLC0415
            )

            settle_or_attach_forwarded_task(app, tid)

    answered = claim_question_transition(
        app,
        child_qid,
        "answered",
        answer=forwarded.answer,
        selected_options=list(forwarded.selected_options),
        answer_metadata=dict(forwarded.answer_metadata),
    )
    if answered is None:
        _terminate("forwarded_child_question_gone")
        return
    # Owner-specific resolution (plan-exit / elicitation) — same dispatcher as the route.
    if resolve_answered_question(app, deps, child_sid, answered):
        # Bind the task to the outcome: if a child turn launched (resume / plan-exit
        # that resumes) settle at its completion; if none launched (plan-exit exit_only —
        # answered + honored, child idle) terminalize SUCCESS + admit, never leak the slot.
        _settle_or_attach(task_id)
        return
    child_sess = app.state.sessions.get(child_sid) if child_sid else None
    if child_sess is None or not answered.metadata.get("resume_on_answer"):
        _terminate("forwarded_child_not_resumable")
        return
    app.state.sessions.update(child_sid, metadata_patch={"pending_user_question_id": ""})
    deps.start_background_user_turn(
        child_sid,
        child_sess,
        deps.ask_user_resume_text(answered),
        metadata={
            "ask_user_question_id": answered.id,
            "ask_user_answer": answered.answer,
            "ask_user_selected_options": answered.selected_options,
            "ask_user_resume": True,
        },
        prev_status=getattr(child_sess, "status", "waiting_user"),
    )
    _settle_or_attach(task_id)  # attach _on_child_done to the resumed turn (never strand)
