"""Answer an agent question with an ordinary composer message, attachments included.

The answer route (``POST .../questions/{id}/answer``) carries text and option
picks only, so an answer with a file could not reach the agent (#1448 gap 1).
Instead of growing a second attachment pipeline, the composer's own message IS
the answer: a ``POST /messages`` whose client metadata names the question
(:data:`ANSWERS_QUESTION_KEY`) goes through the one message-acceptance path
(parts, uploads, references, start-or-steer delivery), and this owner module

1. validates the question before acceptance (pending, raised in this session),
2. gives the accepted message the agent-facing answer header and the
   ``ask_user_resume`` metadata the resume path already uses (so the client
   folds the message into the question's answered record), and
3. settles the question as answered by that message -- the message's own turn
   is the resume, so no second resume turn is started.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException

from clio_agent.gact.types import ErrorEnvelope, ErrorInfo, Part

logger = logging.getLogger(__name__)

__all__ = [
    "ANSWERS_QUESTION_KEY",
    "QuestionAnswerMessage",
    "prepare_question_answer",
    "prepare_queued_question_answer",
    "settle_question_answer",
    "settle_queued_question_answer",
]

#: Client metadata key on ``POST /messages``: the question this message answers.
ANSWERS_QUESTION_KEY = "answers_question_id"


@dataclass(frozen=True)
class QuestionAnswerMessage:
    """What acceptance needs to turn a composer message into a question's answer."""

    question_id: str
    model_text_prefix: str
    metadata: dict[str, Any]


def _refuse(status_code: int, error: str, message: str, **details: Any) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail=ErrorEnvelope(
            error=ErrorInfo(error=error, message=message, details=details, recoverable=True)
        ).model_dump(exclude_none=True),
    )


def prepare_question_answer(
    app: Any, sid: str, metadata: dict[str, Any], *, queued: bool = False
) -> QuestionAnswerMessage | None:
    """Validate the question a message answers; ``None`` for an ordinary message.

    Raises:
        HTTPException: 404 when the question is not in this session, 409 when it
            is no longer pending.
    """

    question_id = str(metadata.get(ANSWERS_QUESTION_KEY) or "").strip()
    if not question_id:
        return None
    question = app.state.user_questions.get(question_id)
    if question is None or question.session_id != sid:
        raise _refuse(
            404,
            "question_not_found",
            "That question is not waiting in this conversation.",
            session_id=sid,
            question_id=question_id,
        )
    if question.status != "pending":
        raise _refuse(
            409,
            "question_already_resolved",
            "That question was already answered or dismissed.",
            session_id=sid,
            question_id=question_id,
            status=question.status,
        )
    if question.response_mode == "async" and not queued:
        raise _refuse(
            422,
            "async_question_needs_queue",
            "Send this answer through the message queue so the agent can keep working.",
            session_id=sid,
            question_id=question.id,
        )
    if (question.metadata or {}).get("variants_id"):
        # A drafts pick names one draft (selected_options), which a message cannot
        # carry: it is answered through the answer route, the message text its comment.
        raise _refuse(
            422,
            "drafts_question_needs_pick",
            "Pick one of the drafts to answer this question; a comment can go with it.",
            session_id=sid,
            question_id=question_id,
        )
    return QuestionAnswerMessage(
        question_id=question.id,
        model_text_prefix=f"[Answer to agent question]\nQuestion: {question.prompt}\nAnswer:\n",
        metadata={
            "ask_user_resume": True,
            "ask_user_question_id": question.id,
            "ask_user_prompt": question.prompt,
            "ask_user_source_turn_id": question.turn_id,
            "ask_user_attempt_id": question.attempt_id,
            "ask_user_caller": question.metadata.get("caller", {}),
            "ask_user_answer_message": True,
        },
    )


def prepare_queued_question_answer(
    app: Any, sid: str, metadata: dict[str, Any]
) -> QuestionAnswerMessage | None:
    """Validate a composer answer that must join the ordinary future-message queue."""
    from clio_agent.gact.messaging import RESERVED_CLIENT_METADATA_KEYS, raise_on_reserved_metadata

    raise_on_reserved_metadata(sid, metadata)
    answer = prepare_question_answer(app, sid, metadata, queued=True)
    if answer is None:
        return None
    question = app.state.user_questions[answer.question_id]
    if question.response_mode != "async":
        raise _refuse(
            422,
            "blocking_question_needs_answer",
            "Answer this question now to continue the paused work.",
            session_id=sid,
            question_id=question.id,
        )
    return QuestionAnswerMessage(
        question_id=answer.question_id,
        model_text_prefix=answer.model_text_prefix,
        metadata={
            key: value
            for key, value in answer.metadata.items()
            if key not in RESERVED_CLIENT_METADATA_KEYS
        }
        | {"question_response_mode": "async"},
    )


def _attachments(parts: list[Part]) -> list[dict[str, str]]:
    """The non-text parts a person attached, as display rows for the record."""

    rows: list[dict[str, str]] = []
    for part in parts:
        if part.type == "text":
            continue
        rows.append(
            {
                "type": part.type,
                "name": str(getattr(part, "name", "") or getattr(part, "label", "") or ""),
                "media_type": str(part.media_type or ""),
                "resource_id": str(part.resource_id or ""),
            }
        )
    return rows


def settle_question_answer(
    app: Any, sid: str, answer: QuestionAnswerMessage, message_id: str
) -> None:
    """Mark the question answered by the accepted message (its turn is the resume)."""

    message = next((row for row in app.state.messages.get(sid, []) if row.id == message_id), None)
    _settle_answer(app, sid, answer, message_id, getattr(message, "parts", None) or [])


def settle_queued_question_answer(
    app: Any, sid: str, answer: QuestionAnswerMessage, message_id: str, parts: list[Part]
) -> None:
    """Settle an async answer only after its files and references are safely queued."""
    _settle_answer(app, sid, answer, message_id, parts, queued=True)


def _settle_answer(
    app: Any,
    sid: str,
    answer: QuestionAnswerMessage,
    message_id: str,
    parts: list[Part],
    *,
    queued: bool = False,
) -> None:
    from clio_agent.gact.elicitation_bridge import claim_question_transition
    from clio_agent.gact.events import Event
    from clio_agent.gact.user_question_resume import settled_question_metadata

    text = "\n".join(part.text for part in parts if part.type == "text").strip()
    attachments = _attachments(parts)
    updated = claim_question_transition(
        app,
        answer.question_id,
        "answered",
        answer=text,
        answer_metadata={
            **({"attachments": attachments} if attachments else {}),
            "queued_message_id" if queued else "answer_message_id": message_id,
        },
        answered_by="human",
        answer_already_delivered=True,
    )
    if updated is None:
        # A concurrent answer or dismissal won; the message still reached the agent
        # as an ordinary message. Typed and logged, never silent.
        logger.warning(
            "question_answer_message_superseded session=%s question=%s message=%s",
            sid,
            answer.question_id,
            message_id,
        )
        return
    app.state.sessions.update(sid, metadata_patch=settled_question_metadata(app, sid, updated))
    for event_type, payload in (
        ("user_question.answered", updated.model_dump(exclude_none=True)),
        (
            "user_question.resumed",
            {
                "question_id": updated.id,
                "session_id": sid,
                "queued_user_message_id": message_id,
                "source_turn_id": updated.turn_id,
            },
        ),
    ):
        app.state.bus.publish(Event(type=event_type, session_id=sid, payload=payload))
