"""Question presentation and trusted input over the common Core representation."""

from __future__ import annotations

from typing import Any

from clio_agent.arc.task_types import QuestionDefinition, QuestionReply, TaskSnapshot
from clio_agent.gact.permission_delivery import attended_session_id
from clio_agent.gact.questions import ObsoleteQuestionGate
from clio_agent.gact.task_access import TaskAccess, TaskCaller
from clio_agent.gact.types import UserQuestion


def prepare_question_gate(app: Any, task: TaskSnapshot, definition: QuestionDefinition) -> None:
    """Install approval projections before Question posting can admit an answer."""
    if definition.source == "hook_defer":
        from clio_agent.gact.hooks.turn_approval import restore_gate as restore_hook_gate

        valid = restore_hook_gate(app, task)
    elif definition.source == "plan_exit":
        from clio_agent.gact.plan_exit_resolution import restore_gate as restore_plan_gate

        valid = restore_plan_gate(app, task, definition)
    else:
        return
    if not valid:
        raise ObsoleteQuestionGate("Approval no longer addresses its owning assignment/input")


def release_cancelled_assignment_gate(app: Any, assignment: TaskSnapshot) -> None:
    """Release only this settled assignment's Question projection, preserving policy gates."""
    history = app.state.task_history
    with history.serialized():
        latest = history.latest_assignment(assignment.owner.session_id)
        session = app.state.sessions.get(assignment.owner.session_id)
        if latest is None or latest.handle != assignment.handle or session is None:
            return
        gate = session.metadata.get("execution_gate")
        question = history.task_snapshot(gate) if isinstance(gate, str) and gate else None
        if (
            question is not None
            and question.kind == "Question"
            and question.tool in {"plan_exit", "hook_defer"}
            and question.owner.assignment_id == assignment.handle
        ):
            app.state.sessions.update(
                session.id,
                metadata_patch={
                    "execution_gate": "",
                    "pending_user_question_id": "",
                    "pending_defer_permission_id": "",
                },
            )


def question_view(app: Any, task: TaskSnapshot) -> dict[str, Any]:
    """Project an existing Question; no presentation record owns its lifecycle."""
    if task.kind != "Question" or task.details is None:
        raise ValueError("Question content is unavailable")
    history = app.state.task_history
    definition = QuestionDefinition.model_validate(history.read_content(task.details))
    answer = (
        QuestionReply.model_validate(history.read_content(task.result))
        if task.raw_status == "answered" and task.result is not None
        else QuestionReply()
    )
    metadata = definition.metadata
    receipt = history.committed_notification_input(
        task.owner.session_id, f"outcome:{task.handle}:{task.owner.session_id}"
    )
    return {
        "id": task.handle,
        "task_handle": task.handle,
        "session_id": task.owner.session_id,
        "owner_session_id": task.owner.session_id,
        "attended_session_id": attended_session_id(app, task.owner.session_id),
        "prompt": definition.prompt,
        "status": task.raw_status,
        "kind": definition.kind,
        "response_mode": "async",
        "options": [
            option.model_dump(mode="json", exclude={"schema_version"})
            for option in definition.options
        ],
        "allow_freeform": definition.allow_freeform,
        "created_at": task.created_at,
        "updated_at": task.updated_at,
        "expires_at": definition.expires_at or "",
        "answer": answer.answer,
        "selected_options": list(answer.selected_options),
        "answer_metadata": answer.metadata,
        "answer_comment": answer.comment,
        "answer_input": receipt.model_dump(
            mode="json",
            include={"id", "session_id", "assignment_id", "iteration_id", "turn_id", "boundary"},
        )
        if receipt is not None
        else None,
        "source": definition.source,
        "metadata": metadata,
        "turn_id": str(metadata.get("source_user_message_id") or task.invocation_id),
        "attempt_id": str(metadata.get("attempt_id") or ""),
        "audience": metadata.get("audience"),
        "agent_elicitation_routing": metadata.get("agent_elicitation_routing") or None,
        "agent_elicitation_fallback_detail": metadata.get("agent_elicitation_fallback_detail")
        or None,
        "answered_by": _answered_by(app, task, answer),
    }


def _answered_by(app: Any, task: TaskSnapshot, answer: QuestionReply) -> str | None:
    """Who produced the answer: an agent, the run itself (nobody watching), or a person."""
    if task.raw_status != "answered":
        return None
    if answer.responder_session:
        return "agent"
    from clio_agent.gact.unattended import is_unattended  # noqa: PLC0415

    # An unattended run answers every Question the moment it is posted.
    return "unattended" if is_unattended(app, task.owner.session_id) else "human"


def question_row(app: Any, handle: str) -> UserQuestion:
    """Decode a shipped UI row from a single common snapshot, without mirroring it."""
    task = app.state.task_history.task_snapshot(handle)
    if task is None:
        raise ValueError("Question unavailable")
    return UserQuestion.model_validate(question_view(app, task))


def render_answer_input(app: Any, caller: TaskCaller, handle: str, text: str) -> dict[str, Any]:
    """Render authorized answer media for the existing recipient-input commitment."""
    from clio_agent.gact.message_submission import prepare_references_sync
    from clio_agent.gact.messaging import queued_user_input
    from clio_agent.gact.types import Message, Part, PostMessageRequest

    history = app.state.task_history
    task = TaskAccess(history, caller).require(handle, "read")
    if task.kind != "Question" or task.raw_status != "answered" or task.result is None:
        return {"text": text, "source": "question_answer", "actor": "algorithm"}
    definition = QuestionDefinition.model_validate(history.read_content(task.details))
    if definition.source == "mcp_elicitation":
        return {"text": text, "source": "question_answer", "actor": "algorithm"}
    reply = QuestionReply.model_validate(history.read_content(task.result))
    if not reply.parts:
        return {"text": text, "source": "question_answer", "actor": "algorithm"}
    session = app.state.sessions.get(caller.session_id)
    if session is None:
        raise ValueError("Question answer recipient is unavailable")
    prepared = prepare_references_sync(
        app,
        session,
        PostMessageRequest(text=text, parts=[Part.model_validate(part) for part in reply.parts]),
    )
    # This transient value is only the native renderer's adapter. It is never
    # stored as another transcript message or mailbox item.
    message = Message(
        id=task.handle,
        session_id=caller.session_id,
        role="user",
        created_at=task.terminal_at or task.updated_at,
        updated_at=task.updated_at,
        parts=list(prepared.parts),
    )
    rendered = queued_user_input(
        app, caller.session_id, {"message": message.model_dump(mode="json"), "text": text}
    )
    return {**rendered, "source": "question_answer", "actor": "algorithm"}


def render_collected_answers(
    app: Any, caller: TaskCaller, handles: tuple[str, ...]
) -> tuple[dict[str, Any], ...]:
    """Reconstruct native answer parts from authorized, immutable collected results."""
    rendered = []
    history = app.state.task_history
    for handle in dict.fromkeys(handles):
        task = TaskAccess(history, caller).require(handle, "read")
        if task.kind != "Question" or task.raw_status != "answered" or task.result is None:
            continue
        reply = QuestionReply.model_validate(history.read_content(task.result))
        if reply.parts:
            rendered.append(
                render_answer_input(app, caller, handle, f"Collected answer attachments: {handle}")
            )
    return tuple(rendered)


def answer_question(
    app: Any, caller: TaskCaller, handle: str, reply: QuestionReply
) -> TaskSnapshot:
    """Authorize answer references, then delegate arbitration to QuestionCommands."""
    from clio_agent.gact.message_submission import prepare_references_sync
    from clio_agent.gact.types import Part, PostMessageRequest

    task = TaskAccess(app.state.task_history, caller).require(handle, "control")
    if task.kind != "Question":
        raise ValueError("Question is unavailable")
    session = app.state.sessions.get(task.owner.session_id)
    if session is None:
        raise ValueError("Question owner is unavailable")
    definition = QuestionDefinition.model_validate(
        app.state.task_history.read_content(task.details)
    )
    if definition.source == "plan_exit":
        if not caller.human_action:
            raise PermissionError("Plan approval requires the authenticated human control channel")
        from clio_agent.gact.plan_exit_resolution import normalize_plan_exit_reply

        reply = normalize_plan_exit_reply(definition, reply)
    if definition.source == "hook_defer":
        from clio_agent.gact.hooks.turn_approval import restore_gate

        if not caller.human_action:
            raise PermissionError("Hook approval requires the authenticated human control channel")
        if task.terminal_at is None and not restore_gate(app, task):
            raise ValueError("Hook approval no longer addresses its retained input")
        reply = reply.model_copy(
            update={
                "responder_human": caller.human_id,
                "responder_session": None,
                "metadata": {"grantor": "user"},
            }
        )
    elicitation = definition.metadata.get("elicitation", {})
    if definition.source == "mcp_elicitation" and isinstance(elicitation, dict):
        from clio_agent.gact.elicitation_schema import build_form_content

        metadata = dict(reply.metadata)
        fields = (definition.requested_schema or {}).get("properties", {})
        if not isinstance(fields, dict):
            raise ValueError("The MCP form's property definitions are malformed")
        action = (
            metadata.pop("elicitation_action", reply.action)
            if "elicitation_action" not in fields
            else reply.action
        )
        if elicitation.get("mode") == "url" and not caller.human_action:
            raise PermissionError("URL consent requires the authenticated human control channel")
        comment = reply.answer  # the person's words, unless they ARE a single field's value
        if elicitation.get("mode") == "form" and action == "accept":
            raw_fields = elicitation.get("fields", [])
            if not isinstance(raw_fields, list):
                raise ValueError("The MCP form's field definitions are malformed")
            form_fields = [field for field in raw_fields if isinstance(field, dict)]
            if len(form_fields) != len(raw_fields):
                raise ValueError("The MCP form's field definitions are malformed")
            built = {"selected_options": reply.selected_options, "answer_metadata": metadata}
            metadata = build_form_content(form_fields, answer=reply.answer, **built)
            if metadata != build_form_content(form_fields, answer="", **built):
                comment = ""
        reply = QuestionReply.model_validate(
            {**reply.model_dump(), "metadata": metadata, "action": action, "comment": comment}
        )
    if reply.parts:
        prepared = prepare_references_sync(
            app,
            session,
            PostMessageRequest(
                text=reply.answer, parts=[Part.model_validate(part) for part in reply.parts]
            ),
        )
        reply = reply.model_copy(
            update={"parts": tuple(part.model_dump(mode="json") for part in prepared.parts)}
        )
    with app.state.task_history.serialized():
        task = TaskAccess(app.state.task_history, caller).require(task.handle, "control")
        if definition.source == "plan_exit" and task.terminal_at is None:
            prepare_question_gate(app, task, definition)
        if (
            definition.source == "hook_defer"
            and task.terminal_at is None
            and not restore_gate(app, task)
        ):
            raise ValueError("Hook approval input changed before answer acceptance")
        if definition.source == "hook_defer":
            from clio_agent.gact.hooks.turn_approval import binding, validate_decision

            validate_decision(
                app,
                binding(app, task),
                reply.action == "accept" and reply.selected_options == ("allow",),
            )
        answered = app.state.questions.answer(task.handle, reply)
    resolve_plan_question(app, answered)
    return answered


def resolve_plan_question(app: Any, task: TaskSnapshot) -> None:
    """Repair the existing plan decision from any durably closed approval Question."""
    from clio_agent.gact.routes.plan_interactions import is_plan_exit_question

    if task.terminal_at is None:
        return
    outcome = app.state.task_history.read_content(task.result) if task.result is not None else None
    if isinstance(outcome, dict) and outcome.get("reason") == "obsolete_gate":
        # Rebuilding a closed stale header cannot replay approval or touch a successor's gate.
        with app.state.task_history.serialized():
            session = app.state.sessions.get(task.owner.session_id)
            if session is not None and session.metadata.get("execution_gate") == task.handle:
                app.state.sessions.update(
                    session.id,
                    status="idle" if session.status == "waiting_user" else None,
                    metadata_patch={
                        key: ""
                        for key in (
                            "execution_gate",
                            "pending_user_question_id",
                            "pending_defer_permission_id",
                        )
                        if session.metadata.get(key) == task.handle
                    },
                )
        return
    if task.tool == "hook_defer":
        from clio_agent.gact.hooks.turn_approval import resolve

        resolve(app, task)
        return
    if task.tool == "plan_exit":
        definition = QuestionDefinition.model_validate(
            app.state.task_history.read_content(task.details)
        )
        from clio_agent.gact.plan_exit_resolution import restore_gate

        if not restore_gate(app, task, definition):
            return
    session = app.state.sessions.get(task.owner.session_id)
    if session is None or session.metadata.get("execution_gate") != task.handle:
        return
    row = question_row(app, task.handle)
    if is_plan_exit_question(row):
        from clio_agent.gact.plan_exit_resolution import resolve_plan_exit_answer

        resolve_plan_exit_answer(app, app.state.gact_deps, session.id, row)
        app.state.conversations.runner.signal(session.id)
