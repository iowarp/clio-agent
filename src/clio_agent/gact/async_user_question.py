"""Non-blocking native questions stored in the ordinary question ledger."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException

from clio_agent.gact.events import Event
from clio_agent.gact.runtime.globals import _emit_semantic_event, _new_question_id
from clio_agent.gact.types import UserQuestion, UserQuestionOption
from clio_agent.gact.user_question_ledger import record_user_question


def queue_async_answer(app: Any, question: UserQuestion) -> str:
    """Save an answer in the composer's FIFO before it becomes terminal."""
    from clio_agent.gact.message_intents import QueueCapacityError, QueuedMessage
    from clio_agent.gact.messaging import _ask_user_resume_text, raise_on_reserved_metadata
    from clio_agent.gact.types import ErrorEnvelope, ErrorInfo, MessageBehavior, ModelRef, Part

    message_id = f"queued_answer_{question.id}"
    raise_on_reserved_metadata(question.session_id, question.answer_metadata)
    existing = app.state.message_intents.get_queued(question.session_id, message_id)
    if existing is not None:
        return existing.id
    session = app.state.sessions.get(question.session_id)
    source_message = next(
        (
            row
            for row in app.state.messages.get(question.session_id, [])
            if row.id == question.turn_id
        ),
        None,
    )
    behavior = MessageBehavior.model_validate(
        source_message.metadata["behavior"]
        if source_message is not None and source_message.metadata.get("behavior")
        else {
            "execution_mode": "plan"
            if session is not None and session.mode == "plan"
            else "execute",
            "confirmation_policy": session.approval_mode if session is not None else "ask",
        }
    )
    intent = QueuedMessage(
        id=message_id,
        session_id=question.session_id,
        client_message_id=message_id,
        idempotency_key=message_id,
        parts=[Part(type="text", text=_ask_user_resume_text(question))],
        model=(
            ModelRef.model_validate(source_message.metadata["effective_model"])
            if source_message is not None and source_message.metadata.get("effective_model")
            else session.model
            if session and session.model
            else ModelRef()
        ),
        behavior=behavior,
        metadata={
            **question.answer_metadata,
            "ask_user_question_id": question.id,
            "ask_user_prompt": question.prompt,
            "ask_user_answer": question.answer,
            "ask_user_selected_options": question.selected_options,
            "ask_user_source_turn_id": question.turn_id,
            "ask_user_caller": question.metadata.get("caller", {}),
            "question_response_mode": "async",
        },
    )
    try:
        row = app.state.message_intents.create_queued(intent)
    except QueueCapacityError as exc:
        raise HTTPException(
            status_code=429,
            detail=ErrorEnvelope(
                error=ErrorInfo(
                    error="queue_capacity_exceeded",
                    message="The message queue is full. Remove or send a queued message, then answer again.",
                    details={"session_id": question.session_id, "limit": exc.limit},
                    recoverable=True,
                )
            ).model_dump(exclude_none=True),
        ) from exc
    return row.id


def publish_async_question(app: Any, pending: dict[str, Any], turn_id: str) -> UserQuestion:
    """Publish a durable question without changing the owning turn's status."""
    from clio_agent.gact.ask_user_tool import arm_ask_user_deadline

    now = datetime.now(timezone.utc).isoformat()
    row = UserQuestion(
        id=_new_question_id(),
        session_id=pending["owner_session_id"],
        owner_session_id=pending["owner_session_id"],
        attended_session_id=pending["attended_session_id"],
        prompt=pending["question"],
        response_mode="async",
        kind=pending["kind"],
        options=[UserQuestionOption(**option) for option in pending["choices"]],
        allow_freeform=pending["allow_freeform"],
        created_at=now,
        updated_at=now,
        expires_at=pending["expires_at"],
        source="native",
        turn_id=turn_id,
        metadata={
            key: pending[key]
            for key in (
                "reason",
                "caller",
                "tool_name",
                "invocation_id",
                "task_id",
                "a2ui_surface_id",
                "a2ui_answer_action",
            )
        }
        | {"resume_on_answer": True},
    )
    record_user_question(app, row)
    _emit_semantic_event(
        app,
        row.session_id,
        "user_question.created",
        turn_id=turn_id,
        status="pending",
        summary="Agent asked a question and can continue working.",
        subject={"question_id": row.id},
        payload=row.model_dump(exclude_none=True),
    )
    app.state.bus.publish(
        Event(
            type="user_question.created",
            session_id=row.session_id,
            payload=row.model_dump(exclude_none=True),
        )
    )
    arm_ask_user_deadline(app, row)
    return row
