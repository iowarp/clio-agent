"""Response ratings stored in ARC/clio-core, separate from model context.

Each change is an immutable record. A cleared rating is a tombstone, so later
evaluation can distinguish an unrated answer from feedback the user withdrew.
"""

from __future__ import annotations

import hashlib
import threading
from datetime import datetime, timedelta, timezone
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from clio_agent.arc.storage import ARCStore

Rating = Literal["good", "bad"]
KIND = "response_feedback"


class ResponseFeedback(BaseModel):
    """Versioned evaluation evidence attached to one exact assistant response."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    feedback_id: UUID
    previous_feedback_id: UUID | None = None
    rating: Rating | None
    created_at: str = ""
    session_id: str
    message_id: str
    turn_id: str
    workspace_id: str = ""
    prompt_message_id: str = ""
    prompt_text: str = ""
    response_text: str
    response_sha256: str
    message_created_at: str
    stop_reason: str
    model_ref: dict[str, str] = Field(default_factory=dict)
    model_ref_source: Literal["message", "prompt_selection", "unknown"] = "unknown"
    source: Literal["user"] = "user"


class FeedbackConflict(ValueError):
    """The submitted revision is stale or its id was reused for another rating."""


def _prefix(session_id: str, message_id: str | None = None) -> str:
    session = hashlib.sha256(session_id.encode()).hexdigest()
    if message_id is None:
        return f"{session}/"
    message = hashlib.sha256(message_id.encode()).hexdigest()
    return f"{session}/{message}/"


class ResponseFeedbackLedger:
    """Persist and read ratings using the same ARCStore as the context plane.

    Writes serialize per ledger, independently of ARC's hot-cache lock. Every
    read comes from the store; a failed write never creates a cached success.
    """

    def __init__(self, store: ARCStore) -> None:
        self._store = store
        self._lock = threading.Lock()

    def history(self, session_id: str, message_id: str | None = None) -> list[ResponseFeedback]:
        """Read immutable changes in recording order, failing on corrupt evidence."""
        rows = [
            ResponseFeedback.model_validate_json(data)
            for _, data in self._store.scan(KIND, _prefix(session_id, message_id))
        ]
        return sorted(rows, key=lambda row: (row.created_at, str(row.feedback_id)))

    def current(self, session_id: str, message_id: str) -> ResponseFeedback | None:
        """Return the latest change, including a cleared rating, if any."""
        rows = self.history(session_id, message_id)
        return rows[-1] if rows else None

    def record(
        self, feedback: ResponseFeedback, *, expected_feedback_id: UUID | None
    ) -> ResponseFeedback:
        """Acknowledge only a completed store write; retries reuse a client UUID.

        The expected id prevents an older browser view from silently replacing a
        newer rating. Retrying an already stored request returns current state.
        """
        with self._lock:
            key = _prefix(feedback.session_id, feedback.message_id) + str(feedback.feedback_id)
            existing = self._store.get(KIND, key)
            current = self.current(feedback.session_id, feedback.message_id)
            if existing is not None:
                saved = ResponseFeedback.model_validate_json(existing)
                if saved.rating != feedback.rating:
                    raise FeedbackConflict("Feedback id was already used for a different rating.")
                return current or saved
            current_id = current.feedback_id if current else None
            if current_id != expected_feedback_id:
                raise FeedbackConflict("The rating changed elsewhere. Refresh it and try again.")
            now = datetime.now(timezone.utc)
            if current:
                now = max(
                    now, datetime.fromisoformat(current.created_at) + timedelta(microseconds=1)
                )
            saved = feedback.model_copy(
                update={"created_at": now.isoformat(), "previous_feedback_id": current_id}
            )
            self._store.put(KIND, key, saved.model_dump_json().encode())
            return saved
