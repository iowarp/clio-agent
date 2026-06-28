"""Message ledger routes for the GACT server (#714).

This concern owns the session message surface: the turn-entry POST plus the
read/search/delete views over a session's stored messages.

* Turn entry -- ``POST /v1/sessions/{sid}/messages``: validate the LM-config
  state + any per-message / per-session model override, gate image parts against
  the active provider's multimodal capability, then ack immediately while the
  agent turn runs in the background via ``deps.start_background_user_turn`` (the
  turn engine in :mod:`clio_agent.gact.turn`). Real LM turns run for minutes, so
  the POST returns the stored user message id rather than holding the connection
  open; clients consume progress over the SSE channel.
* Read -- ``GET /v1/sessions/{sid}/messages`` (newest-first ledger) +
  ``GET /v1/sessions/{sid}/messages/{message_id}`` (SPEC §6.3 drill-down).
* Search -- ``GET /v1/sessions/{sid}/messages/search``: case-insensitive
  substring search with a crude recency-biased ranking.
* Delete -- ``DELETE /v1/sessions/{sid}/messages/{message_id}`` (session-scoped)
  and ``DELETE /v1/messages/{message_id}`` (global, optionally session-hinted);
  both are permission-gated and republish ``message.deleted``.

The module imports only leaf packages (events, providers/config read helpers,
types, stdlib) and never loads :mod:`clio_agent.gact.app`. The cross-concern
helpers the delete + turn-entry paths need (the destructive-action guard, the
ledger replace, the background-turn entrypoint, the active-model ref + its
unsupported-override error, and the agent-not-available error) travel on
:class:`GactDeps`; the model-ref / multimodal read helpers come from
:mod:`clio_agent.gact.providers.config` (their single source).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from fastapi import BackgroundTasks, FastAPI, HTTPException, Response

from clio_agent.gact.events import Event
from clio_agent.gact.providers.config import (
    _active_lm_supports_vision,
    _effective_lm_config,
    _image_part_error,
    _model_ref_is_empty,
    _model_ref_matches_active,
)
from clio_agent.gact.types import (
    ErrorEnvelope,
    ErrorInfo,
    Message,
    PostMessageRequest,
    PostMessageResponse,
)

if TYPE_CHECKING:
    from clio_agent.gact.routes.deps import GactDeps


def register_messages_routes(app: FastAPI, deps: "GactDeps") -> None:
    """Register the session message ledger routes on ``app``.

    Handlers close over the ``app`` argument (FastAPI's decorators need it) and
    reach the session/message state + the live event bus through ``app.state``.
    The cross-concern ``build_app`` helpers (destructive-action guard, ledger
    replace, background-turn entrypoint, active-model ref + its override error,
    agent-not-available error) travel through ``deps`` rather than importing back
    into ``gact.app``. The message-delete helpers below are concern-private.
    """

    def _session_not_found(sid: str) -> HTTPException:
        return HTTPException(
            status_code=404,
            detail=ErrorEnvelope(
                error=ErrorInfo(
                    error="internal_error",
                    message=f"session not found: {sid}",
                    details={"session_id": sid},
                    recoverable=False,
                )
            ).model_dump(exclude_none=True),
        )

    def _message_not_found(message_id: str, *, session_id: str = "") -> HTTPException:
        details = {"message_id": message_id}
        if session_id:
            details["session_id"] = session_id
        return HTTPException(
            status_code=404,
            detail=ErrorEnvelope(
                error=ErrorInfo(
                    error="not_found",
                    message=f"message not found: {message_id}",
                    details=details,
                    recoverable=False,
                )
            ).model_dump(exclude_none=True),
        )

    def _delete_message_from_session(sid: str, message_id: str) -> bool:
        msgs = app.state.messages.get(sid, [])
        for i, message in enumerate(msgs):
            if message.id != message_id:
                continue
            sess = app.state.sessions.get(sid)
            deps.guard_direct_destructive_action(
                app,
                session_id=sid,
                workspace_id=getattr(sess, "workspace_id", ""),
                tool_name="gact.message.delete",
                args={"message_id": message_id, "session_id": sid},
                summary=f"delete message {message_id} from session {sid}",
                reason="user_requested_message_delete",
            )
            msgs.pop(i)
            deps.replace_session_messages(app, sid, msgs)
            if sess is not None:
                app.state.sessions.update(sid, message_count=len(msgs))
            app.state.bus.publish(
                Event(
                    type="message.deleted",
                    session_id=sid,
                    payload={"message_id": message_id, "session_id": sid},
                )
            )
            return True
        return False

    def _live_assistant_message(sid: str) -> Message | None:
        """Return the in-flight assistant projection for reload parity."""

        live_ids = getattr(app.state, "live_assistant_message_ids", {}) or {}
        msg_id = str(live_ids.get(sid) or "")
        if not msg_id:
            return None
        live_parts = list((getattr(app.state, "live_assistant_parts", {}) or {}).get(sid, []))
        if not live_parts:
            return None
        now = datetime.now(timezone.utc).isoformat()
        return Message(
            id=msg_id,
            turn_id=str(getattr(live_parts[0], "turn_id", "") or ""),
            session_id=sid,
            role="assistant",
            created_at=now,
            updated_at=now,
            parts=live_parts,
            metadata={"live": True, "status": "running"},
        )

    # ---- GET /v1/sessions/{sid}/messages/search (BBB27) ---------------

    @app.get("/v1/sessions/{sid}/messages/search")
    async def search_messages(sid: str, q: str = "") -> dict[str, Any]:
        """Case-insensitive substring search across stored messages.

        Returns ``{matches: [{message_id, part_id, snippet, score}]}``.
        Score is a crude recency-biased ranking: newer hits score
        higher (+0.01 per message index) so identical snippets
        surface in turn order.
        """

        sess = app.state.sessions.get(sid)
        if sess is None:
            raise _session_not_found(sid)
        needle = q.strip().lower()
        if not needle:
            return {"matches": []}

        matches: list[dict[str, Any]] = []
        rows = app.state.messages.get(sid, [])
        for idx, m in enumerate(rows):
            for part in m.parts:
                text = (part.text or "").lower()
                i = text.find(needle)
                if i < 0:
                    continue
                # 60-char snippet window centered on the hit.
                start = max(0, i - 30)
                end = min(len(part.text), i + len(needle) + 30)
                snippet = part.text[start:end]
                if start > 0:
                    snippet = "…" + snippet
                if end < len(part.text):
                    snippet = snippet + "…"
                matches.append(
                    {
                        "message_id": m.id,
                        "part_id": part.id,
                        "snippet": snippet,
                        "score": 1.0 + (idx * 0.01),
                    }
                )
        matches.sort(key=lambda r: r["score"], reverse=True)
        return {"matches": matches}

    # ---- POST /v1/sessions/{sid}/messages (BBB9) ---------------------
    # Non-streaming turn: 1 request, 1 response body containing both
    # the stored user message + the assistant's reply. Streaming
    # (SSE on /v1/sessions/{sid}/events) lands in BBB10.

    @app.post("/v1/sessions/{sid}/messages", response_model=PostMessageResponse)
    async def post_message(
        sid: str, req: PostMessageRequest, background_tasks: BackgroundTasks
    ) -> PostMessageResponse:
        """Accept a user message and ack immediately. The agent turn
        runs in the background; clients consume progress via the SSE
        channel (message.created, message.part.delta, ..., message.completed).

        Returning early matters: real LM turns can run for minutes
        (DSPy ReAct loops × 5-15s per Claude call). Holding the POST
        connection open for the whole turn means TUI timeouts, broken
        streaming UX, and no way to surface progress to the user.
        """

        sess = app.state.sessions.get(sid)
        if sess is None:
            raise _session_not_found(sid)
        lm_status = getattr(app.state, "lm_config_status", {}) or {}
        if lm_status.get("state") == "configuring":
            raise HTTPException(
                status_code=503,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="provider_configuring",
                        message=(
                            "LM provider configuration is still in progress; retry after it "
                            "finishes."
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
        if app.state.agent is None:
            raise HTTPException(
                status_code=503,
                detail=deps.agent_not_available_error(app, sid).model_dump(exclude_none=True),
            )

        if (
            req.model is not None
            and not _model_ref_is_empty(req.model)
            and not _model_ref_matches_active(req.model, app)
        ):
            active_model = deps.active_lm_model_ref(app)
            raise HTTPException(
                status_code=501,
                detail=deps.unsupported_model_ref_error(
                    session_id=sid,
                    source="per_message",
                    model_ref=req.model,
                    active_model=active_model,
                ).model_dump(exclude_none=True),
            )

        if not _model_ref_is_empty(sess.model) and not _model_ref_matches_active(sess.model, app):
            active_model = deps.active_lm_model_ref(app)
            if active_model.get("model_id"):
                app.state.sessions.update(sid, model={})
                sess = app.state.sessions.get(sid) or sess
            else:
                raise HTTPException(
                    status_code=501,
                    detail=deps.unsupported_model_ref_error(
                        session_id=sid,
                        source="session",
                        model_ref=sess.model,
                        active_model=active_model,
                    ).model_dump(exclude_none=True),
                )

        user_text = req.extract_text()
        turn_agent_id = req.extract_agent_id().strip()
        image_parts = req.image_parts()
        if image_parts and not _active_lm_supports_vision(app):
            raise HTTPException(
                status_code=501,
                detail=_image_part_error(
                    session_id=sid,
                    image_count=len(image_parts),
                    provider=_effective_lm_config(app),
                ).model_dump(exclude_none=True),
            )
        if not user_text and not image_parts:
            raise HTTPException(
                status_code=422,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="internal_error",
                        message=(
                            "request body carried no text: expected "
                            "parts[] containing a text part or legacy "
                            "top-level text field"
                        ),
                        details={"session_id": sid},
                        recoverable=True,
                    )
                ).model_dump(exclude_none=True),
            )

        # Persist + publish the user message synchronously so by the
        # time the ack returns, GET /messages reflects it. Then mark
        # the session running, then schedule the turn in the
        # background and return.
        user_msg = deps.start_background_user_turn(
            sid,
            sess,
            user_text,
            request_parts=req.parts,
            metadata=req.metadata,
            prev_status="idle",
            turn_agent_id=turn_agent_id,
        )
        # background_tasks parameter is unused but kept on the
        # signature so existing callers (and FastAPI's docs) don't
        # change shape.
        del background_tasks

        return PostMessageResponse(
            message_id=user_msg.id,
            accepted_at=user_msg.created_at,
        )

    @app.get("/v1/sessions/{sid}/messages")
    async def list_messages(sid: str) -> dict[str, Any]:
        """List messages in a session.

        Today: in-memory log populated by POST /messages; returns
        empty when the session exists but has no turns yet. The v0.1
        wire shape (no pagination header, bare array) is what every
        v0.1 backend does; v0.2 clients accept both.
        """

        sess = app.state.sessions.get(sid)
        if sess is None:
            raise _session_not_found(sid)
        # TUI (and SPEC §6.4) expect newest-first with an optional
        # cursor for older pages. We store chronologically so reverse
        # at read time.
        chronological_rows = list(app.state.messages.get(sid, []))
        live_assistant = _live_assistant_message(sid)
        if live_assistant is not None:
            stored_ids = {m.id for m in chronological_rows}
            if live_assistant.id not in stored_ids:
                chronological_rows.append(live_assistant)
        rows = list(reversed(chronological_rows))
        # #731: serialize via ``to_wire`` (not ``model_dump(exclude_none)``) so the
        # reloaded parts are byte-for-byte the slim, arrival-ordered shape the live
        # SSE stream delivered — a reloaded conversation matches what streamed.
        return {
            "messages": [m.to_wire() for m in rows],
            "next_cursor": None,
        }

    @app.get("/v1/sessions/{sid}/messages/{message_id}")
    async def get_message(sid: str, message_id: str) -> dict[str, Any]:
        """SPEC §6.3 drill-down for one stored message."""

        if app.state.sessions.get(sid) is None:
            raise _session_not_found(sid)
        for msg in app.state.messages.get(sid, []):
            if msg.id == message_id:
                return msg.to_wire()  # #731: slim, arrival-ordered parts (matches SSE)
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

    # ---- DELETE /v1/sessions/{sid}/messages/{id} + /v1/messages/{id} --

    @app.delete("/v1/sessions/{sid}/messages/{message_id}")
    async def delete_session_message(sid: str, message_id: str) -> Response:
        if app.state.sessions.get(sid) is None:
            raise _session_not_found(sid)
        if _delete_message_from_session(sid, message_id):
            return Response(status_code=204)
        raise _message_not_found(message_id, session_id=sid)

    @app.delete("/v1/messages/{message_id}")
    async def delete_message(message_id: str, session_id: str = "") -> Response:
        if session_id:
            if app.state.sessions.get(session_id) is None:
                raise _session_not_found(session_id)
            if _delete_message_from_session(session_id, message_id):
                return Response(status_code=204)
            raise _message_not_found(message_id, session_id=session_id)
        for sid in list(app.state.messages):
            if _delete_message_from_session(sid, message_id):
                return Response(status_code=204)
        raise HTTPException(
            status_code=404,
            detail=ErrorEnvelope(
                error=ErrorInfo(
                    error="not_found",
                    message=f"message not found: {message_id}",
                    details={"message_id": message_id},
                    recoverable=False,
                )
            ).model_dump(exclude_none=True),
        )
