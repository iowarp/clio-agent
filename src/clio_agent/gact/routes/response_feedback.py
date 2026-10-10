"""User response ratings, acknowledged only after ARC/clio-core persistence."""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable
from typing import Literal, TypeVar
from uuid import UUID

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict

from clio_agent.arc.response_feedback import (
    FeedbackConflict,
    Rating,
    ResponseFeedback,
    ResponseFeedbackLedger,
)
from clio_agent.gact.off_loop import run_off_loop
from clio_agent.gact.server_boot import process_arc
from clio_agent.gact.types import Message, Session

logger = logging.getLogger(__name__)
T = TypeVar("T")


class RateResponseRequest(BaseModel):
    """Idempotent change against the last feedback id seen by the client."""

    model_config = ConfigDict(extra="forbid")

    feedback_id: UUID
    expected_feedback_id: UUID | None
    rating: Rating | None


class ResponseFeedbackState(BaseModel):
    """Latest stored change; ``rating=null`` means the rating was removed."""

    feedback: ResponseFeedback | None


class ResponseFeedbackHistory(BaseModel):
    """Session-scoped evaluation evidence, including changes and removals."""

    items: list[ResponseFeedback]


def _error(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=status,
        detail={"error": {"error": code, "message": message, "recoverable": status >= 409}},
    )


def _text(message: Message | None) -> str:
    return "\n".join(part.text for part in message.parts if part.type == "text") if message else ""


def register_response_feedback_routes(app: FastAPI) -> None:
    """Register read, change and evaluation-history endpoints without starting turns."""

    def session(sid: str) -> Session:
        value = app.state.sessions.get(sid)
        if value is None:
            raise _error(404, "not_found", "Session not found.")
        return value

    def message(sid: str, message_id: str) -> Message:
        session(sid)
        value = next((row for row in app.state.messages.get(sid, []) if row.id == message_id), None)
        if value is None:
            raise _error(404, "not_found", "Message not found in this session.")
        if value.role != "assistant":
            raise _error(422, "response_not_rateable", "Only assistant responses can be rated.")
        return value

    def ledger() -> ResponseFeedbackLedger:
        arc = process_arc(app)
        value = getattr(arc, "response_feedback", None)
        if not isinstance(value, ResponseFeedbackLedger):
            raise _error(503, "feedback_unavailable", "Response ratings require ARC/clio-core.")
        return value

    async def persist_or_read(operation: Callable[[], T]) -> T:
        try:
            return await run_off_loop(operation)
        except HTTPException:
            raise
        except FeedbackConflict as exc:
            raise _error(409, "feedback_conflict", str(exc)) from exc
        except Exception as exc:
            logger.exception("Response feedback store operation failed")
            raise _error(
                503,
                "feedback_unavailable",
                "Could not confirm the rating in ARC/clio-core. Try again.",
            ) from exc

    @app.get(
        "/v1/sessions/{sid}/messages/{message_id}/feedback", response_model=ResponseFeedbackState
    )
    async def get_response_feedback(sid: str, message_id: str) -> ResponseFeedbackState:
        """Read the durable rating for an assistant message."""
        message(sid, message_id)
        value = await persist_or_read(lambda: ledger().current(sid, message_id))
        return ResponseFeedbackState(feedback=value)

    @app.put(
        "/v1/sessions/{sid}/messages/{message_id}/feedback", response_model=ResponseFeedbackState
    )
    async def rate_response(
        sid: str, message_id: str, body: RateResponseRequest
    ) -> ResponseFeedbackState:
        """Save a rating plus recorded answer/prompt provenance to clio-core."""
        reply = message(sid, message_id)
        if not reply.stop_reason or reply.metadata.get("live"):
            raise _error(
                409, "response_not_settled", "Wait for the response to finish before rating."
            )
        prompt = next(
            (
                row
                for row in app.state.messages.get(sid, [])
                if row.id == reply.turn_id and row.role == "user"
            ),
            None,
        )
        recorded_model = reply.metadata.get("effective_model")
        model_source: Literal["message", "prompt_selection", "unknown"] = "message"
        if not isinstance(recorded_model, dict):
            recorded_model = prompt.metadata.get("effective_model") if prompt else None
            model_source = "prompt_selection"
        response_text = _text(reply)
        record = ResponseFeedback(
            feedback_id=body.feedback_id,
            rating=body.rating,
            session_id=sid,
            message_id=message_id,
            turn_id=reply.turn_id,
            workspace_id=session(sid).workspace_id,
            prompt_message_id=prompt.id if prompt else "",
            prompt_text=_text(prompt),
            response_text=response_text,
            response_sha256=hashlib.sha256(response_text.encode()).hexdigest(),
            message_created_at=reply.created_at,
            stop_reason=reply.stop_reason,
            model_ref={
                key: str(recorded_model[key])
                for key in ("provider_id", "model_id", "variant")
                if isinstance(recorded_model, dict) and recorded_model.get(key)
            },
            model_ref_source=model_source if isinstance(recorded_model, dict) else "unknown",
        )
        saved = await persist_or_read(
            lambda: ledger().record(record, expected_feedback_id=body.expected_feedback_id)
        )
        return ResponseFeedbackState(feedback=saved)

    @app.get("/v1/sessions/{sid}/response-feedback", response_model=ResponseFeedbackHistory)
    async def response_feedback_history(sid: str) -> ResponseFeedbackHistory:
        """Expose the stored history for later evaluation/export, including removals."""
        session(sid)
        return ResponseFeedbackHistory(items=await persist_or_read(lambda: ledger().history(sid)))
