"""Turn-ending hook approvals adapt the existing Core Question owner."""

from __future__ import annotations

import asyncio
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from clio_agent.arc.task_types import (
    AssignmentDetails,
    DeferredCompletion,
    MailItem,
    QuestionDefinition,
    QuestionOption,
    QuestionReply,
    TaskSnapshot,
)
from clio_agent.gact import context
from clio_agent.gact.conversation_assignments import ConversationOutcome
from clio_agent.gact.permission_delivery import publish_permission_event
from clio_agent.gact.runtime.permission_actions import STANDARD_ANSWERS
from clio_agent.gact.session_input import accept_session_input, input_delivery

SOURCE = "hook_defer"


class HookApproval(BaseModel):
    """Immutable hook/input binding retained inside the Question definition."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    event: Literal["Stop", "UserPromptSubmit"]
    assignment: str
    context_epoch: str
    origin: MailItem | None = None
    resume_text: str
    previous_status: str
    turn_id: str = ""
    completion: DeferredCompletion | None = None


def binding(app: Any, task: TaskSnapshot) -> HookApproval:
    """Decode trusted hook facts from the canonical Question, never a UI row."""
    definition = QuestionDefinition.model_validate(
        app.state.task_history.read_content(task.details)
    )
    if task.kind != "Question" or definition.source != SOURCE:
        raise ValueError("This is not a turn-ending hook approval")
    return HookApproval.model_validate(definition.metadata.get("hook_approval"))


def suspend(
    app: Any,
    sid: str,
    event: str,
    reason: str,
    resume_text: str,
    previous_status: str,
    *,
    completion: ConversationOutcome | None = None,
) -> str:
    """Confirm the approval and exact input before installing its admission gate."""
    history = app.state.task_history
    with history.serialized():
        turn = context.current_turn()
        assignment = history.task_snapshot(turn.assignment_handle)
        if assignment is None or assignment.owner.session_id != sid:
            raise ValueError("Hook approval has no verified assignment")
        app.state.conversations.require_admission(assignment)
        details = (
            AssignmentDetails.model_validate(history.read_content(assignment.details))
            if assignment.details
            else AssignmentDetails()
        )
        origin = (
            history.mail_item(sid, turn.input_notification_id)
            if event == "UserPromptSubmit"
            else None
        )
        if event == "UserPromptSubmit" and origin is None:
            raise ValueError("Hook approval has no exact retained input")
        captured = HookApproval.model_validate(
            {
                "event": event,
                "assignment": assignment.handle,
                "context_epoch": details.context_epoch,
                "origin": origin,
                "resume_text": resume_text,
                "previous_status": previous_status,
                "turn_id": turn.turn_id,
                "completion": DeferredCompletion(
                    result=history.write_content(sid, completion.error or completion.outputs),
                    context=completion.context,
                    failed=completion.error is not None,
                )
                if event == "Stop" and completion is not None
                else None,
            }
        )
        question = app.state.questions.create(
            assignment.owner,
            QuestionDefinition(
                source=SOURCE,
                kind="choice",
                allow_freeform=False,
                prompt=reason or f"{event} hook requests approval.",
                options=(
                    QuestionOption(label="Allow", value="allow"),
                    QuestionOption(label="Deny", value="deny"),
                ),
                metadata={"hook_approval": captured.model_dump(mode="json")},
            ),
            invocation_id=f"hook:{event}:{origin.id + ':' + str(origin.revision) if origin else turn.turn_id}",
        )
        if origin is not None and origin.delivery == "eligible":
            history.update_mail(
                input_delivery(origin, held=True, reason="hook_deferred"),
                expected_revision=origin.revision,
            )
        restore_gate(app, question)
        return question.handle


def _completion_applied(app: Any, captured: HookApproval, session_id: str) -> bool:
    """Recognize only this frozen approval's exact completion on its original handle."""
    completion = captured.completion
    current = app.state.task_history.task_snapshot(captured.assignment)
    if (
        captured.event != "Stop"
        or completion is None
        or current is None
        or current.owner.session_id != session_id
        or current.terminal_at is None
        or current.cancellation_requested_at is not None
        or current.status != ("failed" if completion.failed else "completed")
        or current.result != completion.result
        or current.details is None
    ):
        return False
    details = AssignmentDetails.model_validate(app.state.task_history.read_content(current.details))
    return (
        details.context_epoch == captured.context_epoch
        and details.disposition == "complete"
        and details.last_output == completion.result
        and (completion.context is None or details.context == completion.context)
    )


def restore_gate(app: Any, task: TaskSnapshot) -> bool:
    """Repair interrupted gate installation only from the exact retained correlation."""
    history, captured = app.state.task_history, binding(app, task)
    with history.serialized():
        session = app.state.sessions.get(task.owner.session_id)
        current = history.latest_assignment(task.owner.session_id)
        frozen = app.state.questions.resolution(task.handle)
        if (
            session is not None
            and session.metadata.get("execution_gate") == task.handle
            and frozen is not None
            and frozen.get("approved") is True
            and frozen.get("binding") == captured.model_dump(mode="json")
            and _completion_applied(app, captured, task.owner.session_id)
        ):
            return True  # Repair gate release without reopening or retargeting a completed run.
        if session is None or current is None or current.handle != captured.assignment:
            return False
        if current.terminal_at is not None or current.cancellation_requested_at is not None:
            return False
        details = (
            AssignmentDetails.model_validate(history.read_content(current.details))
            if current.details
            else AssignmentDetails()
        )
        if details.context_epoch != captured.context_epoch:
            return False
        gate = session.metadata.get("execution_gate")
        if gate and gate != task.handle:
            return False
        if captured.origin is not None and app.state.questions.resolution(task.handle) is None:
            original = history.mail_item(session.id, captured.origin.id)
            if original is None:
                return False
            allowed = (
                captured.origin,
                *(
                    input_delivery(captured.origin, held=True, reason=reason)
                    for reason in ("hook_deferred", "execution_failed_before_input_commit")
                ),
            )
            if original not in allowed:
                return False
            if history.mail_withdrawn(
                session.id, original.id
            ) or original.id in history.acknowledged_notifications(session.id):
                return False
        if gate == task.handle:
            return True
        app.state.sessions.update(
            session.id,
            status="waiting_user",
            metadata_patch={
                "execution_gate": task.handle,
                "pending_defer_permission_id": task.handle,
            },
        )
        return True


def project(app: Any, task: TaskSnapshot, row: dict[str, Any]) -> None:
    """Expose the existing permission card as a rebuildable Question projection."""
    captured = HookApproval.model_validate(row["metadata"].get("hook_approval"))
    answered = row["answer_metadata"]
    permission = {
        "id": task.handle,
        "session_id": task.owner.session_id,
        "kind": "turn_defer",
        "hook_event": captured.event,
        "summary": row["prompt"],
        "defer_reason": row["prompt"],
        "created_at": task.created_at,
        "status": "pending" if task.terminal_at is None else "resolved",
        "action": "allow" if row["selected_options"] == ["allow"] else "deny",
        "grantor": answered.get("grantor", "user"),
        "reason": "hook_defer_pending",
        "updated_at": task.updated_at,
    }
    prior = app.state.permissions.get(task.handle)
    if prior is not None and prior.get("updated_at", "") > task.updated_at:
        return
    app.state.permissions[task.handle] = permission
    publish_permission_event(
        app,
        "permission.requested" if task.terminal_at is None else "permission.resolved",
        owner_session_id=task.owner.session_id,
        payload=permission,
    )


def validate_decision(app: Any, captured: HookApproval, approved: bool) -> None:
    """An approval cannot substitute another segment's last output for missing evidence."""
    if approved and captured.event == "Stop":
        if captured.completion is None:
            raise ValueError("Deferred completion has no verified exact output; gate retained")
        app.state.task_history.read_content(captured.completion.result)


def respond(app: Any, task: TaskSnapshot, action: str, grantor: str) -> dict[str, Any]:
    """Accept an authenticated decision through QuestionCommands, then repair its effect."""
    if grantor != "user":
        raise PermissionError("Turn-ending hook approval requires the authenticated human")
    from clio_agent.gact.task_access import TaskAccess, TaskCaller

    TaskAccess(
        app.state.task_history, TaskCaller("", app.state.human_principal, human_action=True)
    ).require(task.handle, "control")
    if action not in STANDARD_ANSWERS:
        raise ValueError("Unsupported hook approval action")
    captured = binding(app, task)
    validate_decision(app, captured, action.startswith("allow"))
    if captured.event == "UserPromptSubmit" and captured.origin is None:
        raise ValueError("Legacy hook approval has no verified original input; gate retained")
    reply = QuestionReply(
        selected_options=("allow" if action.startswith("allow") else "deny",),
        responder_human=app.state.human_principal,
        metadata={"grantor": grantor},
    )
    with app.state.task_history.serialized():
        if task.terminal_at is None and not restore_gate(app, task):
            raise ValueError("Hook approval no longer addresses its retained input")
        answered = app.state.questions.answer(task.handle, reply)
        resolve(app, answered)
    from clio_agent.gact.question_surface import question_view

    project(app, answered, question_view(app, answered))
    return app.state.permissions[task.handle]


def _decision(app: Any, task: TaskSnapshot, captured: HookApproval) -> dict[str, Any]:
    questions, history = app.state.questions, app.state.task_history
    frozen = questions.resolution(task.handle)
    if frozen is not None:
        return frozen
    reply = (
        QuestionReply.model_validate(history.read_content(task.result))
        if task.raw_status == "answered"
        else None
    )
    approved = (
        reply is not None
        and reply.action == "accept"
        and reply.selected_options == ("allow",)
        and bool(reply.responder_human)
    )
    return questions.freeze_resolution(
        task.handle,
        {
            "approved": approved,
            "binding": captured.model_dump(mode="json"),
        },
    )


def resolve(app: Any, task: TaskSnapshot) -> None:
    """Apply a frozen decision; input release is idempotent and never a delivery ACK."""
    if task.terminal_at is None:
        return
    history, owner, captured = app.state.task_history, app.state.conversations, binding(app, task)
    sid = task.owner.session_id
    with history.serialized():
        session = app.state.sessions.get(sid)
        existing = app.state.questions.resolution(task.handle)
        if session is None:
            raise ValueError("Hook approval session is unavailable")
        if existing is not None and session.metadata.get("execution_gate") != task.handle:
            # A previous effect released this gate. Never reinstall it or target
            # a successor merely because an old answer is read/retried.
            return
        if not restore_gate(app, task):
            raise ValueError("Hook approval no longer has its exact gate/input binding")
        frozen = _decision(app, task, captured)

        def release() -> None:
            with history.serialized():
                current = app.state.sessions.get(sid)
                if current is not None and current.metadata.get("execution_gate") == task.handle:
                    app.state.sessions.update(
                        sid,
                        status="idle",
                        metadata_patch={
                            "execution_gate": "",
                            "pending_defer_permission_id": "",
                        },
                    )
            owner.runner.signal(sid)

        if captured.event == "Stop" and frozen["approved"]:
            if _completion_applied(app, captured, sid):
                release()
                return

            async def complete() -> None:
                if captured.completion is None:
                    raise ValueError(
                        "Deferred completion has no verified exact output; gate retained"
                    )
                await owner.complete_deferred(
                    sid,
                    captured.assignment,
                    context_epoch=captured.context_epoch,
                    completion=captured.completion,
                )
                await history.io.run(release)

            async def finish() -> None:
                try:
                    await complete()
                except Exception:
                    if not history.requires_recovery:
                        raise
                    owner.recovery.defer("hook-completion:" + task.handle, complete)

            def register() -> None:
                identity = "hook-completion:" + task.handle
                if identity not in owner._controls:
                    owner.retain_control(identity, asyncio.create_task(finish()))

            owner._loop.call_soon_threadsafe(register)
            return
        if captured.event == "UserPromptSubmit":
            origin = captured.origin
            if origin is None:
                raise ValueError("Legacy hook approval has no verified original input")
            current = history.mail_item(sid, origin.id)
            if (
                current is None
                or history.mail_withdrawn(sid, origin.id)
                or origin.id in history.acknowledged_notifications(sid)
            ):
                raise ValueError("Hook approval input is no longer pending")
            if (
                frozen["approved"]
                and current.approval_question == task.handle
                and current.content == origin.content
            ):
                pass  # interrupted gate release; the same decision already changed this input
            elif (
                not frozen["approved"]
                and current.content == origin.content
                and current.delivery == "held"
                and current.held_reason == "hook_blocked"
            ):
                pass
            elif current.content != origin.content or current.revision not in {
                origin.revision,
                origin.revision + 1,
            }:
                raise ValueError("Hook approval input changed after capture")
            else:
                history.update_mail(
                    input_delivery(
                        current, held=not frozen["approved"], reason="hook_blocked"
                    ).model_copy(
                        update={"approval_question": task.handle if frozen["approved"] else None}
                    ),
                    expected_revision=current.revision,
                )
        elif task.raw_status == "answered":
            accept_session_input(
                app,
                sid,
                captured.resume_text,
                source=SOURCE,
                metadata={"stop_defer_redrive": True, "question_id": task.handle, "approval": True},
                steer_message_id="hook-resume:" + task.handle,
                handle=task.handle,
            )
        release()


def approved_input(app: Any, sid: str, input_id: str) -> bool:
    """Recognize only the retained input released by its confirmed human decision."""
    history = app.state.task_history
    with history.serialized():
        item = history.mail_item(sid, input_id)
        if item is None:
            return False
        handle = item.approval_question
        task = history.task_snapshot(handle) if isinstance(handle, str) else None
        if task is None or task.tool != SOURCE or task.owner.session_id != sid:
            return False
        frozen = app.state.questions.resolution(task.handle)
        captured = HookApproval.model_validate(frozen["binding"]) if frozen else None
        return bool(
            frozen
            and frozen["approved"]
            and captured is not None
            and captured.origin is not None
            and captured.origin.id == item.id
            and captured.origin.content == item.content
        )
