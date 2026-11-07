"""Turn retry routes: list a session's attempts and retry a turn from a message (#333).

A retry is a ``POST /messages`` sibling: it records a :class:`TurnAttempt` and, when
asked to execute, queues the source user message again (with optional notes, and
refusing a different model than the active one). A2UI actions reuse the same handler
through ``app.state.retry_turn_action`` so both share its provenance and gates.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI, HTTPException

from clio_agent.gact import context_reference_retry
from clio_agent.gact.events import Event
from clio_agent.gact.message_submission import _apply_a2ui_client_metadata_guards
from clio_agent.gact.message_submission_errors import session_not_found
from clio_agent.gact.messaging import raise_on_reserved_metadata
from clio_agent.gact.off_loop import join_off_loop
from clio_agent.gact.runtime.globals import _new_attempt_id
from clio_agent.gact.runtime.retention import enforce_dict_bound
from clio_agent.gact.types import (
    ErrorEnvelope,
    ErrorInfo,
    Message,
    ModelRef,
    RetryTurnRequest,
    TurnAttempt,
)

if TYPE_CHECKING:
    from clio_agent.gact.routes.deps import GactDeps


def register_turn_retry_routes(app: FastAPI, deps: "GactDeps") -> None:
    """Register ``GET /v1/sessions/{sid}/attempts`` and the message retry route."""

    def _retry_source_user_message(messages: list[Message], source: Message) -> Message | None:
        if source.role == "user":
            return source
        try:
            source_index = next(idx for idx, msg in enumerate(messages) if msg.id == source.id)
        except StopIteration:
            return None
        for msg in reversed(messages[:source_index]):
            if msg.role == "user":
                return msg
        return None

    @app.get("/v1/sessions/{sid}/attempts")
    async def list_turn_attempts(sid: str) -> dict[str, Any]:
        if app.state.sessions.get(sid) is None:
            raise session_not_found(sid)
        rows = [a for a in app.state.turn_attempts.values() if a.session_id == sid]
        rows.sort(key=lambda a: a.created_at, reverse=True)
        return {"attempts": [a.model_dump(exclude_none=True) for a in rows]}

    @app.post(
        "/v1/sessions/{sid}/messages/{message_id}/retry",
        response_model=TurnAttempt,
        status_code=202,
    )
    async def retry_turn(sid: str, message_id: str, req: RetryTurnRequest) -> TurnAttempt:
        sess = app.state.sessions.get(sid)
        if sess is None:
            raise session_not_found(sid)
        messages = app.state.messages.get(sid, [])
        source = next((m for m in messages if m.id == message_id), None)
        if source is None:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="not_found",
                        message=f"message not found: {message_id}",
                        details={"session_id": sid, "message_id": message_id},
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )
        # #1057 B2 (BLOCKER): retry is a POST /messages sibling -- reject a
        # smuggled control key in ``req.metadata`` via the shared chokepoint.
        raise_on_reserved_metadata(sid, req.metadata)
        _apply_a2ui_client_metadata_guards(app, sid, req.metadata)
        model_payload = (req.model or ModelRef()).model_dump()
        if req.provider_id:
            model_payload["provider_id"] = req.provider_id
        if req.model_id:
            model_payload["model_id"] = req.model_id
        active_model = deps.active_lm_model_ref(app)
        model_changed = bool(
            (model_payload.get("provider_id") or model_payload.get("model_id"))
            and (
                model_payload.get("provider_id", "") != active_model.get("provider_id", "")
                or model_payload.get("model_id", "") != active_model.get("model_id", "")
            )
        )
        warning = ""
        if model_changed:
            warning = (
                "Retrying with a different model/provider may recompute provider-side KV "
                "cache, increase time to first token, increase latency/cost, and produce "
                "different tool or reasoning behavior."
            )
        execution_blocked_reason = ""
        retry_user_msg: Message | None = None
        source_user = _retry_source_user_message(messages, source)
        if req.execute:
            if app.state.agent is None:
                raise HTTPException(
                    status_code=503,
                    detail=deps.agent_not_available_error(app, sid).model_dump(exclude_none=True),
                )
            lm_status = getattr(app.state, "lm_config_status", {}) or {}
            if lm_status.get("state") == "configuring":
                raise HTTPException(
                    status_code=503,
                    detail=ErrorEnvelope(
                        error=ErrorInfo(
                            error="provider_configuring",
                            message=(
                                "LM provider configuration is still in progress; retry after "
                                "it finishes."
                            ),
                            details={
                                "session_id": sid,
                                "operation_id": lm_status.get("operation_id", ""),
                                "provider": lm_status.get("provider", ""),
                                "model": lm_status.get("model", ""),
                                "recovery_actions": ["wait", "check_lm_provider_status", "retry"],
                            },
                            recoverable=True,
                        )
                    ).model_dump(exclude_none=True),
                )
            if not context_reference_retry.retryable_user_message(source_user):
                execution_blocked_reason = "source_user_message_not_found"
            elif model_changed:
                envelope = deps.unsupported_model_ref_error(
                    session_id=sid,
                    source="retry",
                    model_ref=model_payload,
                    active_model=active_model,
                )
                envelope.error.details.update(
                    {
                        "message_id": message_id,
                        "notes_present": bool(req.notes.strip()),
                        "warning": warning,
                        "recovery_actions": [
                            "put_global_lm_provider",
                            "retry_without_model_override",
                            "retry_after_provider_switch",
                            "exit",
                        ],
                    }
                )
                raise HTTPException(
                    status_code=422,
                    detail=envelope.model_dump(exclude_none=True),
                )
        # Refuse concurrent retry execution; preserve the attempt's blocked reason.
        if req.execute and not execution_blocked_reason and app.state.turn_runner.busy(sid):
            execution_blocked_reason = "session_busy"
        retry_parts = None
        if req.execute and not execution_blocked_reason and source_user is not None:
            retry_parts = await context_reference_retry.authorize_retry_parts(
                app, sess, source_user, req.notes
            )
        now_iso = datetime.now(timezone.utc).isoformat()
        attempt = TurnAttempt(
            id=_new_attempt_id(),
            session_id=sid,
            source_message_id=message_id,
            status=(
                "queued"
                if req.execute and not execution_blocked_reason
                else ("failed" if req.execute else "recorded")
            ),
            created_at=now_iso,
            updated_at=now_iso,
            notes=req.notes,
            model=ModelRef(**model_payload),
            warning=warning,
            metadata={
                **req.metadata,
                "source_message_role": source.role,
                "source_user_message_id": source_user.id if source_user is not None else "",
                "active_model": active_model,
                "retry_protocol": "queued_for_execution" if req.execute else "recorded_for_replay",
                "execution_blocked_reason": execution_blocked_reason,
            },
        )
        app.state.turn_attempts[attempt.id] = attempt
        if req.execute and not execution_blocked_reason and source_user is not None:
            retry_text = context_reference_retry.retry_user_text(
                context_reference_retry.message_text(source_user), req.notes
            )
            assert retry_parts is not None
            retry_user_msg = await join_off_loop(
                deps.start_background_user_turn,
                sid,
                sess,
                retry_text,
                metadata={
                    **req.metadata,
                    "retry_attempt_id": attempt.id,
                    "retry_source_message_id": message_id,
                    "retry_source_user_message_id": source_user.id,
                    "retry_notes": req.notes,
                },
                prev_status=sess.status,
                request_parts=retry_parts,
                supersedes_input=source_user.id,
            )
            attempt = attempt.model_copy(
                update={
                    "metadata": {
                        **attempt.metadata,
                        "queued_user_message_id": retry_user_msg.id,
                    }
                }
            )
            app.state.turn_attempts[attempt.id] = attempt
        enforce_dict_bound(app, app.state.turn_attempts, "turn_attempts", session_id=sid)
        app.state.bus.publish(
            Event(
                type="turn.retry_requested",
                session_id=sid,
                payload=attempt.model_dump(exclude_none=True),
            )
        )
        return attempt

    # A2UI shares REST retry provenance, busy/provider gates and source-user recovery.
    app.state.retry_turn_action = retry_turn
