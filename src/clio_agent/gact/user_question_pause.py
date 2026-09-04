"""Turn-ending user-question pause and exact-session ownership transition."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from clio_agent.gact.enrichment import _finalize_context_frame
from clio_agent.gact.events import Event
from clio_agent.gact.messaging import _ask_user_options_from_action, _coerce_ask_user_action
from clio_agent.gact.runtime.globals import _emit_semantic_event, _new_question_id
from clio_agent.gact.turn_stream import settle_turn_transcript
from clio_agent.gact.types import UserQuestion
from clio_agent.gact.user_question_ledger import record_user_question

if TYPE_CHECKING:
    from collections.abc import Callable

    from clio_agent.gact.turn_state import TurnState


def maybe_pause_for_user(
    state: "TurnState",
    pred: Any,
    *,
    update_retry_attempt: "Callable[..., None]",
) -> bool:
    """Pause one turn on an authoritative native or model-produced question."""

    session = state.app.state.sessions.get(state.sid)
    pending = (
        (getattr(session, "metadata", None) or {}).get("pending_ask_user")
        if session is not None
        else None
    )
    ask_user_action = (
        dict(pending)
        if isinstance(pending, dict) and pending and not pending.get("surfaced")
        else _coerce_ask_user_action(pred)
    )
    if state.error_info is not None or not ask_user_action:
        return False
    now_iso = datetime.now(timezone.utc).isoformat()
    options = _ask_user_options_from_action(ask_user_action)
    kind_raw = str(ask_user_action.get("kind") or "").strip()
    kind = kind_raw if kind_raw in {"freeform", "choice", "confirmation"} else ""
    if not kind:
        kind = "choice" if options and not ask_user_action.get("allow_freeform") else "freeform"
    from clio_agent.gact.permission_delivery import attended_session_id  # noqa: PLC0415

    owner = str(ask_user_action.get("owner_session_id") or "") or state.sid
    attended = str(ask_user_action.get("attended_session_id") or "") or attended_session_id(
        state.app, owner
    )
    question = UserQuestion(
        id=_new_question_id(),
        session_id=state.sid,
        owner_session_id=owner,
        attended_session_id=attended,
        prompt=str(ask_user_action["question"]),
        status="pending",
        kind=kind,  # type: ignore[arg-type]
        options=options,
        allow_freeform=bool(ask_user_action.get("allow_freeform", False)),
        created_at=now_iso,
        updated_at=now_iso,
        expires_at=str(ask_user_action.get("expires_at") or ""),
        source="native" if isinstance(pending, dict) and pending else "orchestrator_action",
        turn_id=state.user_msg.id,
        attempt_id=state.retry_attempt_id,
        metadata={
            **dict(ask_user_action.get("metadata") or {}),
            "reason": ask_user_action.get("reason", ""),
            "caller": ask_user_action.get("caller", {}),
            "task_id": ask_user_action.get("task_id", ""),
            "invocation_id": ask_user_action.get("invocation_id", ""),
            "resume_on_answer": True,
            "source_user_message_id": state.user_msg.id,
            "source_user_text": state.user_text,
            "selected_agent": state.selected_agent,
            "route_source": state.route_source,
            "route_reason": state.route_reason,
        },
    )
    record_user_question(state.app, question)
    _emit_semantic_event(
        state.app,
        state.sid,
        "user_question.created",
        turn_id=state.turn_id,
        trace_id=state.trace_id,
        status="waiting_user",
        summary="Agent requested user input before continuing.",
        actor={"agent_id": state.selected_agent or state.invocation_agent_id},
        subject={"question_id": question.id},
        payload=question.model_dump(exclude_none=True),
    )
    metadata_patch: dict[str, Any] = {"pending_user_question_id": question.id}
    if isinstance(pending, dict) and pending:
        metadata_patch["pending_ask_user"] = {
            **pending,
            "surfaced": True,
            "question_id": question.id,
            "created_at": question.created_at,
            "question_record": question.model_dump(exclude_none=True),
        }
    updated = state.app.state.sessions.update(
        state.sid,
        status="waiting_user",
        message_count=len(state.app.state.messages.get(state.sid, [])),
        metadata_patch=metadata_patch,
    )
    if isinstance(pending, dict) and pending:
        from clio_agent.gact.ask_user_tool import arm_ask_user_deadline  # noqa: PLC0415

        arm_ask_user_deadline(state.app, question)
    _finalize_context_frame(
        state.app,
        state.sid,
        state.context_frame["id"],
        "",
        "completed",
        error_info=None,
    )
    state.bus.publish(
        Event(
            type="user_question.created",
            session_id=state.sid,
            payload=question.model_dump(exclude_none=True),
        )
    )
    state.bus.publish(
        Event(
            type="session.status_changed",
            session_id=state.sid,
            payload={
                "session_id": state.sid,
                "status": "waiting_user",
                "prev_status": "running",
                "updated_at": updated.updated_at if updated is not None else "",
                "pending_user_question_id": question.id,
            },
        )
    )
    if state.retry_attempt_id:
        update_retry_attempt(
            "completed",
            metadata_patch={
                "ask_user_question_id": question.id,
                "stop_reason": "waiting_user",
            },
        )
    settle_turn_transcript(state)
    return True


__all__ = ["maybe_pause_for_user"]
