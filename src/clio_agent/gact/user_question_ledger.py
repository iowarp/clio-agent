"""The write and recovery funnel for the ``user_questions`` ledger.

``app.state.user_questions`` is written from five producers (the native ask tool's
restart replay, the elicitation bridge, plan-mode, the per-session question route
and the pause path) and read in full by the interaction projection. It was the
only such in-memory ledger with no retention bound or restart recovery, so a
long-lived server accumulated every question it had ever asked and a restarted
server lost the causal interaction history that the transcript still referenced.

Routing every write through here adds a bounded mirror to the owning session and
gives the live ledger the same terminal-first, typed-eviction policy as
``permissions`` and ``turn_attempts``. Pending questions are retained, while a
bounded resolved history keeps causal MCP activity inspectable after restart.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from clio_agent.gact.interaction_types import UserQuestion
from clio_agent.gact.runtime.retention import enforce_dict_bound

if TYPE_CHECKING:
    from fastapi import FastAPI

__all__ = [
    "USER_QUESTIONS_METADATA_KEY",
    "record_user_question",
    "restore_user_questions",
]

USER_QUESTIONS_METADATA_KEY = "user_questions"
_RESOLVED_HISTORY_LIMIT = 100
_PERSIST_LOCK = threading.RLock()
logger = logging.getLogger(__name__)


def record_user_question(app: "FastAPI", question: UserQuestion) -> UserQuestion:
    """Store one question in the live ledger and its bounded session mirror."""

    # Commit the durable row before making the new state observable in the live
    # projection. Terminal transitions can trigger resume/release work as soon as
    # readers see them; they must never expose a state that a process crash would
    # immediately forget.
    _persist_question(app, question)
    app.state.user_questions[question.id] = question
    enforce_dict_bound(
        app,
        app.state.user_questions,
        "user_questions",
        session_id=str(getattr(question, "session_id", "") or ""),
    )
    return question


def restore_user_questions(app: "FastAPI") -> int:
    """Rehydrate durable question rows before pending-question recovery runs."""

    restored = 0
    for session in app.state.sessions.list():
        metadata = session.metadata if isinstance(session.metadata, Mapping) else {}
        rows = metadata.get(USER_QUESTIONS_METADATA_KEY)
        if not isinstance(rows, Mapping):
            continue
        for raw in rows.values():
            if not isinstance(raw, Mapping):
                continue
            try:
                question = UserQuestion.model_validate(raw)
            except ValueError:
                logger.warning(
                    "durable user question ignored reason=invalid_record session=%s",
                    session.id,
                )
                continue
            current = app.state.user_questions.get(question.id)
            if current is not None and current.updated_at >= question.updated_at:
                continue
            app.state.user_questions[question.id] = question
            restored += 1
    enforce_dict_bound(app, app.state.user_questions, "user_questions")
    return restored


def _persist_question(app: "FastAPI", question: UserQuestion) -> None:
    """Write the authoritative row into its owning session's bounded mirror."""

    sessions = getattr(app.state, "sessions", None)
    session_id = question.owner_session_id or question.session_id
    if sessions is None or not session_id:
        return
    with _PERSIST_LOCK:
        session = sessions.get(session_id)
        if session is None:
            logger.warning(
                "user question held process-locally reason=session_absent question=%s session=%s",
                question.id,
                session_id,
            )
            return
        metadata = session.metadata if isinstance(session.metadata, Mapping) else {}
        raw_rows = metadata.get(USER_QUESTIONS_METADATA_KEY)
        rows = dict(raw_rows) if isinstance(raw_rows, Mapping) else {}
        rows[question.id] = question.model_dump(exclude_none=True)
        rows = _bounded_rows(rows)
        updated = sessions.update(
            session_id,
            metadata_patch={USER_QUESTIONS_METADATA_KEY: rows},
        )
        if updated is None:
            logger.warning(
                "user question held process-locally reason=session_deleted question=%s session=%s",
                question.id,
                session_id,
            )


def _bounded_rows(rows: dict[str, Any]) -> dict[str, Any]:
    """Retain every pending row and the newest bounded resolved history."""

    pending: list[tuple[str, Mapping[str, Any]]] = []
    resolved: list[tuple[str, Mapping[str, Any]]] = []
    for question_id, raw in rows.items():
        if not isinstance(raw, Mapping):
            continue
        item = (question_id, raw)
        if str(raw.get("status") or "pending") == "pending":
            pending.append(item)
        else:
            resolved.append(item)
    resolved.sort(key=lambda item: str(item[1].get("updated_at") or ""), reverse=True)
    return {key: dict(value) for key, value in [*pending, *resolved[:_RESOLVED_HISTORY_LIMIT]]}
