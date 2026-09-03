"""Shared helpers for the per-session ask-user question routes.

Lifted out of ``routes/sessions.py`` (a baselined god file) because none of them
need that module's closure over the route factory: they are pure, or take ``app``
explicitly. Keeping them here lets the question routes grow without regrowing the
file the size ratchet is holding down.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import HTTPException

from clio_agent.gact.types import (
    CreateUserQuestionRequest,
    ErrorEnvelope,
    ErrorInfo,
    UserQuestion,
    UserQuestionOption,
)

if TYPE_CHECKING:
    from fastapi import FastAPI

__all__ = [
    "normalize_question_options",
    "pending_user_questions",
    "question_already_resolved",
    "question_not_found",
]


def question_not_found(sid: str, question_id: str) -> HTTPException:
    """404 for a question id that names nothing in this session."""

    return HTTPException(
        status_code=404,
        detail=ErrorEnvelope(
            error=ErrorInfo(
                error="not_found",
                message=f"user question not found: {question_id}",
                details={"session_id": sid, "question_id": question_id},
                recoverable=False,
            )
        ).model_dump(exclude_none=True),
    )


def question_already_resolved(sid: str, question_id: str) -> HTTPException:
    """409: the question left ``pending`` before this write (concurrent settle)."""

    return HTTPException(
        status_code=409,
        detail=ErrorEnvelope(
            error=ErrorInfo(
                error="bad_request",
                message="user question is already resolved",
                details={"session_id": sid, "question_id": question_id},
                recoverable=False,
            )
        ).model_dump(exclude_none=True),
    )


def pending_user_questions(app: "FastAPI", sid: str) -> list[UserQuestion]:
    """Every still-pending question owned by one session."""

    return [
        row
        for row in app.state.user_questions.values()
        if row.session_id == sid and row.status == "pending"
    ]


def normalize_question_options(req: CreateUserQuestionRequest) -> list[UserQuestionOption]:
    """Give a confirmation question its implied yes/no options."""

    if req.kind == "confirmation" and not req.options:
        return [
            UserQuestionOption(label="Yes", value="yes", description=""),
            UserQuestionOption(label="No", value="no", description=""),
        ]
    return list(req.options)
