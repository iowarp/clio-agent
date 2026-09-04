"""Resume ordinary ask-user turns after an authoritative answer."""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from typing import Any

from clio_agent.gact.events import Event
from clio_agent.gact.loop_inbox import enqueue_user_steer

logger = logging.getLogger(__name__)


def resume_answered_question(
    app: Any,
    deps: Any,
    sid: str,
    question: Any,
    *,
    has_pending: bool,
    set_session_status: Callable[..., None],
) -> None:
    """Resume or durably defer one answered native ``ask_user`` turn."""

    if has_pending:
        return
    session = app.state.sessions.get(sid)
    should_resume = bool(question.metadata.get("resume_on_answer")) and session is not None
    metadata_patch: dict[str, Any] = {"pending_user_question_id": ""}
    pending_ask = (session.metadata or {}).get("pending_ask_user") if session is not None else None
    if (
        isinstance(pending_ask, Mapping)
        and str(pending_ask.get("question_id") or "") == question.id
    ):
        metadata_patch["pending_ask_user"] = {
            **pending_ask,
            "resolved_status": "answered",
            "resolved_at": question.updated_at,
        }
    resume_metadata = {
        "ask_user_question_id": question.id,
        "ask_user_prompt": question.prompt,
        "ask_user_answer": question.answer,
        "ask_user_selected_options": question.selected_options,
        "ask_user_source_turn_id": question.turn_id,
        "ask_user_attempt_id": question.attempt_id,
        "ask_user_caller": question.metadata.get("caller", {}),
        "ask_user_resume": True,
    }
    agent_initializing = app.state.agent is None
    if should_resume and (agent_initializing or app.state.turn_runner.busy(sid)):
        enqueue_user_steer(
            app,
            sid,
            deps.ask_user_resume_text(question),
            {**resume_metadata, "question_id": question.id},
        )
        app.state.sessions.update(
            sid,
            status="idle" if agent_initializing else None,
            metadata_patch=metadata_patch,
        )
        reason = "agent_initializing" if agent_initializing else "session_busy"
        app.state.bus.publish(
            Event(
                type="user_question.resume_deferred",
                session_id=sid,
                payload={"question_id": question.id, "session_id": sid, "reason": reason},
            )
        )
        logger.info(
            "user_question resume deferred reason=%s session_id=%s question_id=%s",
            reason,
            sid,
            question.id,
        )
        return
    if should_resume:
        app.state.sessions.update(sid, metadata_patch=metadata_patch)
        resumed_msg = deps.start_background_user_turn(
            sid,
            session,
            deps.ask_user_resume_text(question),
            metadata=resume_metadata,
            prev_status=session.status if session is not None else "waiting_user",
        )
        app.state.bus.publish(
            Event(
                type="user_question.resumed",
                session_id=sid,
                payload={
                    "question_id": question.id,
                    "session_id": sid,
                    "queued_user_message_id": resumed_msg.id,
                    "source_turn_id": question.turn_id,
                },
            )
        )
        return
    set_session_status(
        sid,
        "idle",
        prev_status=session.status if session is not None else "waiting_user",
        metadata_patch=metadata_patch,
    )


__all__ = ["resume_answered_question"]
