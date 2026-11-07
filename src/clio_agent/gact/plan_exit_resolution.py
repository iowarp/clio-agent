"""Plan-exit answer resolution and resume staging."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict

from clio_agent.arc.task_types import (
    AssignmentDetails,
    QuestionDefinition,
    QuestionReply,
    TaskSnapshot,
)
from clio_agent.gact.plan_exit_resume import approved_plan_resume_text, rejected_plan_resume_text
from clio_agent.gact.planning import PLAN_VARIANT_METADATA_KEY, transition_playbook_to_execution
from clio_agent.gact.session_plan import PLAN_STATUS_KEY

if TYPE_CHECKING:
    from fastapi import FastAPI

    from clio_agent.gact.routes.deps import GactDeps


class PlanApproval(BaseModel):
    """Fixed assignment and plan-request identity retained in its Question."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    assignment: str
    context_epoch: str
    request: dict[str, Any]


def normalize_plan_exit_reply(
    definition: QuestionDefinition, reply: QuestionReply
) -> QuestionReply:
    """Normalize the plan decision and its advertised auxiliary context modifier."""
    from clio_agent.gact.plan_mode import _PLAN_EXIT_CLEAR_CONTEXT

    choices = {option.value for option in definition.options}
    if not reply.selected_options and not reply.answer.strip() and "reject" in choices:
        # An empty human plan response keeps the existing safe-rejection policy.
        # Ordinary Questions still require an answer in their unchanged owner.
        return reply.model_copy(update={"selected_options": ("reject",)})
    if (
        _PLAN_EXIT_CLEAR_CONTEXT not in reply.selected_options
        or _PLAN_EXIT_CLEAR_CONTEXT not in choices
    ):
        return reply
    if len(set(reply.selected_options)) != len(reply.selected_options):
        raise ValueError("An option cannot be selected twice")
    primary = tuple(value for value in reply.selected_options if value != _PLAN_EXIT_CLEAR_CONTEXT)
    if not primary:
        raise ValueError("A plan context modifier requires an approval or rejection decision")
    return reply.model_copy(
        update={
            "selected_options": primary,
            "metadata": {**reply.metadata, "clear_context": True},
        }
    )


def restore_gate(app: Any, task: TaskSnapshot, definition: QuestionDefinition) -> bool:
    """Install a plan gate from confirmed Question content before it is answerable."""
    from clio_agent.gact.plan_mode import _PLAN_EXIT_PENDING_KEY

    history = app.state.task_history
    with history.serialized():
        session = app.state.sessions.get(task.owner.session_id)
        if session is None:
            return False
        gate = session.metadata.get("execution_gate")
        if task.terminal_at is not None and task.raw_status != "answered":
            return gate == task.handle  # Closed input may release its gate, never reinstall it.
        current = history.latest_assignment(task.owner.session_id)
        if current is None or current.terminal_at is not None:
            return False
        if current.cancellation_requested_at is not None:
            return False
        captured = PlanApproval.model_validate(definition.metadata.get("plan_approval"))
        if current.handle != captured.assignment or task.owner.assignment_id != current.handle:
            return False
        frozen = app.state.questions.resolution(task.handle)
        if frozen is not None and gate != task.handle:
            return False  # Already released; historical reads cannot reinstall a gate.
        details = (
            AssignmentDetails.model_validate(history.read_content(current.details))
            if current.details is not None
            else AssignmentDetails()
        )
        if details.context_epoch != captured.context_epoch and frozen is None:
            return False
        if gate and gate != task.handle:
            return False
        if gate == task.handle:
            return True
        app.state.sessions.update(
            session.id,
            status="waiting_user",
            metadata_patch={
                "execution_gate": task.handle,
                "pending_user_question_id": task.handle,
                _PLAN_EXIT_PENDING_KEY: {
                    **captured.request,
                    "surfaced": True,
                    "question_id": task.handle,
                },
            },
        )
        return True


def _stage_plan_exit_resume(
    app: "FastAPI",
    deps: "GactDeps",
    sid: str,
    session: Any,
    text: str,
    metadata: dict[str, Any],
    *,
    question_id: str,
) -> None:
    """Publish approved/revised work into the sole session admission mailbox."""
    from clio_agent.gact.session_input import accept_session_input

    accept_session_input(
        app,
        sid,
        text,
        metadata={**metadata, "approval": True},
        steer_message_id="plan-answer:" + question_id,
        source="plan_exit",
        handle=question_id,
    )


def _stage_plan_revision_message(
    app: "FastAPI",
    deps: "GactDeps",
    sid: str,
    session: Any,
    feedback: str,
    *,
    plan_file: str,
    question_id: str,
) -> None:
    """Retain human revision feedback without launching a competing turn."""
    _stage_plan_exit_resume(
        app,
        deps,
        sid,
        session,
        feedback,
        {"plan_exit_result": "rejected", "plan_file": plan_file},
        question_id=question_id,
    )


def resolve_plan_exit_answer(app: "FastAPI", deps: "GactDeps", sid: str, question: Any) -> None:
    """Apply one explicit plan decision and stage the corresponding lifecycle transition."""

    with app.state.task_history.serialized():
        from clio_agent.gact.plan_mode import _PLAN_EXIT_PENDING_KEY

        session = app.state.sessions.get(sid)
        if session is None or session.metadata.get("execution_gate") != question.id:
            return
        if question.status == "answered":
            _resolve_plan_exit_answer(app, deps, sid, question)
        app.state.sessions.update(
            sid,
            status="idle" if session.status == "waiting_user" else None,
            metadata_patch={
                _PLAN_EXIT_PENDING_KEY: {},
                "pending_user_question_id": "",
                "execution_gate": "",
            },
        )


def _frozen_plan_resolution(app: Any, question: Any) -> dict[str, Any]:
    """Pin the accepted Question decision before effects; never reread a mutable plan."""
    existing = app.state.questions.resolution(question.id)
    if existing is not None:
        return existing
    from clio_agent.gact.plan_mode import _PLAN_EXIT_CLEAR_CONTEXT, _PLAN_EXIT_DECISIONS

    metadata = getattr(question, "metadata", None) or {}
    answer = getattr(question, "answer_metadata", None) or {}
    selected = list(getattr(question, "selected_options", None) or [])
    decision = next((item for item in selected if item in _PLAN_EXIT_DECISIONS), "reject")
    feedback = str(getattr(question, "answer", "") or "").strip()
    path = str(metadata.get("plan_file") or "")
    clear = decision != "reject" and (
        _PLAN_EXIT_CLEAR_CONTEXT in selected or bool(answer.get("clear_context"))
    )
    captured = metadata.get("artifact_ref")
    artifact = (
        dict(captured)
        if isinstance(captured, Mapping)
        else {
            "saved": False,
            "reason": "artifact_not_captured_at_review",
        }
    )
    approved = {
        "artifact_ref": artifact,
        "plan_file": path,
        "content": str(metadata.get("plan_content") or ""),
        "content_status": str(metadata.get("plan_content_status") or "unavailable_at_review"),
    }
    composer = bool(answer.get("composer_user_message"))
    resume_metadata: dict[str, Any] = (
        {"plan_exit_result": "rejected", "plan_file": path}
        if decision == "reject"
        else {
            "plan_exit_result": "approved",
            "plan_exit_mode": decision,
            "plan_exit_context_cleared": clear,
            "plan_file": path,
            "approved_plan": approved,
        }
    )
    resume_text = (
        (feedback if composer and feedback else rejected_plan_resume_text(feedback, path))
        if decision == "reject"
        else approved_plan_resume_text(decision, path, approved)
        if decision != "exit_only"
        else None
    )
    return app.state.questions.freeze_resolution(
        question.id,
        {
            "decision": decision,
            "feedback": feedback,
            "composer_user_message": composer,
            "plan_file": path,
            "clear_context": clear,
            "clear_boundary": question.created_at,
            "context_epoch": question.id if clear else "",
            "approved_plan": approved,
            "resume_metadata": resume_metadata,
            "resume_text": resume_text,
        },
    )


def _resolve_plan_exit_answer(app: "FastAPI", deps: "GactDeps", sid: str, question: Any) -> None:
    """Stage the deterministic decision before releasing its admission gate."""

    from clio_agent.gact.events import Event

    frozen = _frozen_plan_resolution(app, question)
    decision, feedback, plan_file = frozen["decision"], frozen["feedback"], frozen["plan_file"]
    clear_context = frozen["clear_context"]
    answer_meta = {"composer_user_message": frozen["composer_user_message"]}

    if decision == "reject":
        session = app.state.sessions.get(sid)
        app.state.bus.publish(
            Event(
                type="plan_exit.resolved",
                session_id=sid,
                payload={
                    "decision": "reject",
                    "plan_file": plan_file,
                    "composer_user_message": bool(answer_meta.get("composer_user_message")),
                },
            )
        )
        if feedback and answer_meta.get("composer_user_message") is True:
            _stage_plan_revision_message(
                app,
                deps,
                sid,
                session,
                feedback,
                plan_file=plan_file,
                question_id=question.id,
            )
        else:
            _stage_plan_exit_resume(
                app,
                deps,
                sid,
                session,
                frozen["resume_text"],
                {"plan_exit_result": "rejected", "plan_file": plan_file},
                question_id=question.id,
            )
        return

    approval_mode = "auto-edits" if decision == "auto" else "ask"
    app.state.sessions.update(
        sid,
        mode="edit",
        approval_mode=approval_mode,
        metadata_patch={PLAN_VARIANT_METADATA_KEY: "", PLAN_STATUS_KEY: "approved"},
    )
    app.state.bus.publish(
        Event(
            type="session.updated",
            session_id=sid,
            payload={
                "session_id": sid,
                "mode": "edit",
                "approval_mode": approval_mode,
                "reason": "plan_exit_approved",
            },
        )
    )
    transition_playbook_to_execution(app, sid, decision_id=question.id)

    approved_plan = frozen["approved_plan"]
    saved_plan = approved_plan["artifact_ref"]
    cleared = False
    if clear_context:
        from clio_agent.gact.context import after_context_boundary
        from clio_agent.gact.conversation_assignments import clear_model_context

        boundary = frozen["clear_boundary"]
        clear_model_context(app.state.conversations, sid, frozen["context_epoch"], boundary)
        retained = [
            row
            for row in app.state.messages.get(sid, [])
            if after_context_boundary(row.created_at, boundary) is not False
        ]
        app.state.a2ui_store.announce_ledger_clear(sid, "plan_exit_context_cleared")
        deps.replace_session_messages(app, sid, retained)
        app.state.sessions.update(
            sid, message_count=len(retained), metadata_patch={"plan_exit_context_cleared": True}
        )
        cleared = True
    session = app.state.sessions.get(sid)
    resume_metadata = frozen["resume_metadata"]
    app.state.bus.publish(
        Event(
            type="plan_exit.resolved",
            session_id=sid,
            payload={
                "decision": decision,
                "cleared_context": cleared,
                "plan_file": plan_file,
                "artifact_ref": saved_plan,
            },
        )
    )

    if decision == "exit_only":
        app.state.sessions.update(
            sid,
            status="idle",
            metadata_patch={"plan_exit_result": "approved_exit_only"},
        )
        app.state.bus.publish(
            Event(
                type="session.status_changed",
                session_id=sid,
                payload={"session_id": sid, "status": "idle", "prev_status": "waiting_user"},
            )
        )
        return

    _stage_plan_exit_resume(
        app,
        deps,
        sid,
        session,
        frozen["resume_text"],
        resume_metadata,
        question_id=question.id,
    )
