"""Session lifecycle + ask-user/retry routes for the GACT server (#714).

This concern owns the largest GACT surface: the ``/v1/sessions`` lifecycle and the
session-scoped ask-user / retry protocol.

* CRUD -- ``POST/GET/PATCH/DELETE /v1/sessions`` (+ ``GET /v1/sessions/{sid}``):
  create against the workspace store, list with the archive partition, patch the
  mutable mode/title fields, and permission-gated delete (which also drops the
  session's messages, context-file ledger and hot ARC footprint).
* Rollback -- ``POST /v1/sessions/{sid}/undo`` + ``.../rewind``: drop the trailing
  ``count`` messages (undo) or everything past a target message (rewind), both
  permission-gated and republished as ``message.deleted`` + ``session.{op}``.
* Branch/transfer -- ``POST /v1/sessions/{sid}/fork`` (copy a session + its
  messages into a fresh child), ``GET /v1/sessions/{sid}/export`` +
  ``POST /v1/sessions/import`` (portable JSON round-trip).
* Compaction -- ``POST /v1/sessions/{sid}/compact``: summarise the transcript
  through the live agent into an evidence-preserving compact memory, archive the
  originals, store the summary in ARC, and replace the visible ledger.
* Cancel -- ``POST /v1/sessions/{sid}/cancel``: best-effort cooperative cancel of
  an in-flight turn (flip the flag, signal the event, schedule a grace-period task
  cancel) + a ``session.status_changed`` event.
* Ask-user -- ``GET/POST /v1/sessions/{sid}/questions`` + ``.../answer`` +
  ``.../cancel``: the orchestrator's user-question ledger; answering a
  ``resume_on_answer`` question stages a background resume turn.
* Retry -- ``GET /v1/sessions/{sid}/attempts`` +
  ``POST /v1/sessions/{sid}/messages/{message_id}/retry``: record/execute a turn
  retry, optionally kicking a background turn off the source user message.

The fork, question-answer and retry routes drive a background user turn through
``deps.start_background_user_turn`` (the turn engine in
:mod:`clio_agent.gact.turn`). The module imports only leaf packages (events,
runtime, types, stdlib) and never loads :mod:`clio_agent.gact.app`; the shared
cross-concern helpers (ledger replace, ARC release, model-ref
errors, evidence index, resume text) travel on :class:`GactDeps`. The session-
private rollback + ask-user/retry helpers live here.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Optional

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from clio_agent.gact import context as _ctx
from clio_agent.gact.events import Event
from clio_agent.gact.mcp_apps import cleanup_session_mcp_apps
from clio_agent.gact.routes._body import NonObjectBodyError, json_body
from clio_agent.gact.routes.compaction import build_compact_summary_message
from clio_agent.gact.routes.session_filters import filter_session_rows
from clio_agent.gact.runtime.constants import _installed_clio_agent_version
from clio_agent.gact.runtime.globals import (
    _active_semantic_turn_id,
    _emit_semantic_event,
    _new_attempt_id,
    _new_cancellation_attempt_id,
    _new_memory_event_id,
    _new_question_id,
)
from clio_agent.gact.runtime.retention import enforce_dict_bound
from clio_agent.gact.types import (
    AnswerUserQuestionRequest,
    CreateSessionRequest,
    CreateUserQuestionRequest,
    ErrorEnvelope,
    ErrorInfo,
    ListSessionsResponse,
    Message,
    ModelRef,
    RetryTurnRequest,
    Session,
    TurnAttempt,
    UpdateSessionRequest,
    UserQuestion,
    UserQuestionOption,
    Workspace,
)

if TYPE_CHECKING:
    from clio_agent.gact.routes.deps import GactDeps

logger = logging.getLogger(__name__)


def register_sessions_routes(app: FastAPI, deps: "GactDeps") -> None:
    """Register the ``/v1/sessions`` lifecycle + ask-user/retry routes on ``app``.

    Handlers close over the ``app`` argument (FastAPI's decorators need it) and
    reach sessions/messages/questions/attempts state + the live event bus through
    ``app.state``. Cross-concern ``build_app`` helpers (ledger replace,
    context-file/ARC release, model-ref errors, the evidence index, the
    ask-user resume text, the destructive-action guard, and the background-turn
    entrypoint) travel through ``deps`` rather than importing back into
    ``gact.app``. The rollback + ask-user/retry helper closures are concern-private.
    """

    def _session_not_found(sid: str) -> HTTPException:
        return HTTPException(
            status_code=404,
            detail=ErrorEnvelope(
                error=ErrorInfo(
                    error="not_found",
                    message=f"session not found: {sid}",
                    details={"session_id": sid},
                    recoverable=False,
                )
            ).model_dump(exclude_none=True),
        )

    # ---- /v1/sessions CRUD -----------------------------------------

    @app.post("/v1/sessions", response_model=Session)
    async def create_session(req: CreateSessionRequest) -> Session:
        wid = req.workspace_id or "ws_default"
        if app.state.workspaces.get(wid) is None:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="not_found",
                        message=f"workspace not found: {wid}",
                        details={"workspace_id": wid},
                        recoverable=True,
                    )
                ).model_dump(exclude_none=True),
            )
        sess = app.state.sessions.create(
            workspace_id=wid,
            title=req.title,
            metadata=req.metadata,
            model=req.model.model_dump(exclude_none=True) if req.model else None,
            agent=req.agent.model_dump(exclude_none=True) if req.agent else None,
            mode=req.mode,
            edit_mode=req.edit_mode,
            routing_mode=req.routing_mode,
        )
        return Session(**sess.to_wire())

    @app.patch("/v1/sessions/{sid}", response_model=Session)
    async def patch_session(sid: str, req: UpdateSessionRequest) -> Session:
        """Update mutable session fields (title + mode + edit_mode).

        Lets the TUI flip plan ↔ edit ↔ chat ↔ architect mid-
        session without recreating, and rename via the existing
        rename modal.
        """

        sess = app.state.sessions.update(
            sid,
            title=req.title,
            mode=req.mode,
            edit_mode=req.edit_mode,
            routing_mode=req.routing_mode,
            model=req.model.model_dump(exclude_none=True) if req.model else None,
            agent=req.agent.model_dump(exclude_none=True) if req.agent else None,
            # iowarp/gact-tui §audit/E-14: persist pin + archive state.
            metadata_patch=req.metadata,
            archived=req.archived,
        )
        if sess is None:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="not_found",
                        message=f"session not found: {sid}",
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )
        # Publish so live SSE subscribers see mode flips immediately.
        app.state.bus.publish(
            Event(
                type="session.updated",
                session_id=sid,
                payload=Session(**sess.to_wire()).model_dump(exclude_none=True),
            )
        )
        return Session(**sess.to_wire())

    @app.get("/v1/sessions", response_model=ListSessionsResponse)
    async def list_sessions(
        workspace_id: Optional[str] = None,
        include_all_workspaces: bool = False,
        archived: Optional[bool] = None,
        parent_session_id: Optional[str] = None,
    ) -> ListSessionsResponse:
        """List sessions, filtered by workspace scope, archive bucket (audit E-14),
        and optionally fork lineage — ``parent_session_id`` non-empty restricts to
        that parent's direct sub-sessions (#232); omitted/empty is unchanged."""

        effective_workspace_id = workspace_id or (None if include_all_workspaces else "ws_default")
        rows = app.state.sessions.list(workspace_id=effective_workspace_id)
        rows = filter_session_rows(rows, archived=archived, parent_session_id=parent_session_id)
        return ListSessionsResponse(sessions=[Session(**row.to_wire()) for row in rows])

    @app.get("/v1/sessions/{sid}", response_model=Session)
    async def get_session(sid: str, workspace_id: Optional[str] = None) -> Session:
        sess = app.state.sessions.get(sid)
        if sess is None:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="not_found",
                        message=f"session not found: {sid}",
                        details={"session_id": sid},
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )
        if workspace_id and sess.workspace_id != workspace_id:
            raise HTTPException(
                status_code=403,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="permission_error",
                        message="session is outside the requested workspace scope",
                        details={
                            "session_id": sid,
                            "session_workspace_id": sess.workspace_id,
                            "requested_workspace_id": workspace_id,
                            "scope": "other_workspace",
                        },
                        recoverable=True,
                    )
                ).model_dump(exclude_none=True),
            )
        return Session(**sess.to_wire())

    @app.delete("/v1/sessions/{sid}")
    async def delete_session(sid: str) -> Response:
        sess = app.state.sessions.get(sid)
        if sess is None:
            raise _session_not_found(sid)
        deps.guard_direct_destructive_action(
            app,
            session_id=sid,
            workspace_id=sess.workspace_id,
            tool_name="gact.session.delete",
            args={"session_id": sid},
            summary=f"delete session {sid}",
            reason="user_requested_session_delete",
        )
        try:
            await cleanup_session_mcp_apps(app, sid)
        except RuntimeError as exc:
            raise HTTPException(
                status_code=502,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="mcp_app_cleanup_failed",
                        message="session retained because an owned MCP App failed to close",
                        details={"session_id": sid, "reason": str(exc)},
                        recoverable=True,
                    )
                ).model_dump(exclude_none=True),
            ) from exc
        existed = app.state.sessions.delete(sid)
        if not existed:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="not_found",
                        message=f"session not found: {sid}",
                        details={"session_id": sid},
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )
        deps.delete_session_messages(app, sid)
        deps.delete_session_context_files(app, sid)
        deps.release_session_arc(app, sid)
        return Response(status_code=204)

    # ---- Rollback (undo / rewind) -----------------------------------

    def _reject_rollback_while_active(sid: str, sess: Any) -> None:
        if getattr(sess, "status", "") in {"running", "waiting_permission"}:
            raise HTTPException(
                status_code=409,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="conflict",
                        message=f"session {sid} cannot be rolled back while {sess.status}",
                        details={"session_id": sid, "status": sess.status},
                        recoverable=True,
                    )
                ).model_dump(exclude_none=True),
            )

    def _publish_rollback_events(
        sid: str,
        *,
        operation: str,
        deleted_ids: list[str],
        session_payload: dict[str, Any],
        target_message_id: str = "",
        include_target: bool = False,
    ) -> None:
        for message_id in deleted_ids:
            app.state.bus.publish(
                Event(
                    type="message.deleted",
                    session_id=sid,
                    payload={
                        "message_id": message_id,
                        "session_id": sid,
                        "operation": operation,
                    },
                )
            )
        app.state.bus.publish(
            Event(
                type=f"session.{operation}",
                session_id=sid,
                payload={
                    "session_id": sid,
                    "deleted_message_ids": deleted_ids,
                    "target_message_id": target_message_id,
                    "include_target": include_target,
                },
            )
        )
        app.state.bus.publish(
            Event(
                type="session.updated",
                session_id=sid,
                payload=session_payload,
            )
        )

    def _commit_rollback(
        sid: str,
        *,
        operation: str,
        kept_messages: list[Message],
        deleted_messages: list[Message],
        target_message_id: str = "",
        include_target: bool = False,
    ) -> dict[str, Any]:
        deps.replace_session_messages(app, sid, kept_messages)
        deleted_ids = [m.id for m in deleted_messages]
        updated = app.state.sessions.update(
            sid,
            message_count=len(kept_messages),
            status="idle",
            metadata_patch={
                "last_rollback": {
                    "operation": operation,
                    "deleted_message_ids": deleted_ids,
                    "target_message_id": target_message_id,
                    "include_target": include_target,
                    "memory_scope": "gact_visible_transcript_only",
                    "occurred_at": datetime.now(timezone.utc).isoformat(),
                }
            },
        )
        if updated is None:
            raise _session_not_found(sid)
        session_payload = Session(**updated.to_wire()).model_dump(exclude_none=True)
        _publish_rollback_events(
            sid,
            operation=operation,
            deleted_ids=deleted_ids,
            session_payload=session_payload,
            target_message_id=target_message_id,
            include_target=include_target,
        )
        return {
            "session_id": sid,
            "operation": operation,
            "deleted_message_ids": deleted_ids,
            "deleted_messages": deleted_ids,
            "reverted_message_ids": deleted_ids,
            "message_count": len(kept_messages),
            "memory_scope": "gact_visible_transcript_only",
            "session": session_payload,
        }

    @app.post("/v1/sessions/{sid}/undo")
    async def undo_session(sid: str, request: Request) -> dict[str, Any]:
        sess = app.state.sessions.get(sid)
        if sess is None:
            raise _session_not_found(sid)
        _reject_rollback_while_active(sid, sess)
        # Optional free-form body: a malformed or ``null`` payload is treated as
        # ``{}`` (unchanged behavior, now with a structured
        # ``request_body_unparseable`` reason in the trace), but a valid-JSON
        # non-object payload keeps its pre-#772 422 -- undo is destructive and
        # must not proceed on a wrong-shaped body coerced to defaults.
        try:
            body = await json_body(
                request, route="POST /v1/sessions/{sid}/undo", non_object="raise"
            )
        except NonObjectBodyError:
            raise HTTPException(
                status_code=422,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="validation_error",
                        message="undo request body must be an object",
                        details={"session_id": sid},
                        recoverable=True,
                    )
                ).model_dump(exclude_none=True),
            ) from None
        raw_count = body.get("count", body.get("message_count", 1))
        try:
            count = int(raw_count) if isinstance(raw_count, str | int | float) else 1
        except (TypeError, ValueError):
            count = 1
        if count < 1:
            raise HTTPException(
                status_code=422,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="validation_error",
                        message="undo count must be at least 1",
                        details={"session_id": sid, "count": raw_count},
                        recoverable=True,
                    )
                ).model_dump(exclude_none=True),
            )
        messages = list(app.state.messages.get(sid, []))
        deleted = messages[-count:]
        kept = messages[: max(0, len(messages) - count)]
        deps.guard_direct_destructive_action(
            app,
            session_id=sid,
            workspace_id=sess.workspace_id,
            tool_name="gact.session.undo",
            args={"session_id": sid, "count": count},
            summary=f"undo last {count} message(s) in session {sid}",
            reason="user_requested_session_undo",
        )
        return _commit_rollback(
            sid,
            operation="undo",
            kept_messages=kept,
            deleted_messages=deleted,
        )

    @app.post("/v1/sessions/{sid}/rewind")
    async def rewind_session(sid: str, request: Request) -> dict[str, Any]:
        sess = app.state.sessions.get(sid)
        if sess is None:
            raise _session_not_found(sid)
        _reject_rollback_while_active(sid, sess)
        # A malformed body is treated as ``{}`` (unchanged behavior, now with a
        # structured ``request_body_unparseable`` reason in the trace), but a
        # valid-JSON non-object payload -- including ``null``, which rewind's
        # pre-#772 guard never coerced -- keeps its 422: rewind is destructive
        # and must not proceed on a wrong-shaped body coerced to defaults.
        try:
            body = await json_body(
                request,
                route="POST /v1/sessions/{sid}/rewind",
                non_object="raise",
                null_is_empty=False,
            )
        except NonObjectBodyError:
            raise HTTPException(
                status_code=422,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="validation_error",
                        message="rewind request body must be an object",
                        details={"session_id": sid},
                        recoverable=True,
                    )
                ).model_dump(exclude_none=True),
            ) from None
        target_message_id = str(
            body.get("message_id")
            or body.get("target_message_id")
            or body.get("to_message_id")
            or ""
        ).strip()
        if not target_message_id:
            raise HTTPException(
                status_code=422,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="validation_error",
                        message="rewind requires message_id",
                        details={"session_id": sid},
                        recoverable=True,
                    )
                ).model_dump(exclude_none=True),
            )
        include_target = bool(body.get("include_target", False))
        messages = list(app.state.messages.get(sid, []))
        target_index = next(
            (index for index, message in enumerate(messages) if message.id == target_message_id),
            -1,
        )
        if target_index < 0:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="not_found",
                        message=f"message not found: {target_message_id}",
                        details={"session_id": sid, "message_id": target_message_id},
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )
        keep_end = target_index if include_target else target_index + 1
        kept = messages[:keep_end]
        deleted = messages[keep_end:]
        deps.guard_direct_destructive_action(
            app,
            session_id=sid,
            workspace_id=sess.workspace_id,
            tool_name="gact.session.rewind",
            args={
                "session_id": sid,
                "message_id": target_message_id,
                "include_target": include_target,
            },
            summary=f"rewind session {sid} to message {target_message_id}",
            reason="user_requested_session_rewind",
        )
        return _commit_rollback(
            sid,
            operation="rewind",
            kept_messages=kept,
            deleted_messages=deleted,
            target_message_id=target_message_id,
            include_target=include_target,
        )

    # ---- POST /v1/sessions/{sid}/fork (BBB26) -------------------------

    @app.post("/v1/sessions/{sid}/fork")
    async def fork_session(sid: str, request: Request) -> Response:
        """Copy a session + its messages into a fresh session.

        Body (optional): ``{"at_message_id": "<id>", "title": "..."}``
        ``at_message_id`` truncates the copy at + including that
        message (so "branch from this point"). Absent → copy every
        stored message.

        The new session's ``parent_session_id`` points at the source
        so the TUI's sidebar can render the fork hierarchy (the v0.1
        Session already carries that field).
        """

        sess = app.state.sessions.get(sid)
        if sess is None:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="not_found",
                        message=f"session not found: {sid}",
                        details={"session_id": sid},
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )

        body = await json_body(request, route="POST /v1/sessions/{sid}/fork")
        at = body.get("at_message_id") or ""
        title = body.get("title") or f"{sess.title} (fork)"

        src_msgs = list(app.state.messages.get(sid, []))
        if at:
            kept: list[Message] = []
            for m in src_msgs:
                kept.append(m)
                if m.id == at:
                    break
            src_msgs = kept

        new_sess = app.state.sessions.create(
            workspace_id=sess.workspace_id,
            title=title,
            parent_session_id=sid,
        )
        # Deep-copy parts so the fork's message log doesn't alias the
        # source's. Pydantic's model_copy gives us a snapshot.
        deps.replace_session_messages(
            app,
            new_sess.id,
            [m.model_copy(deep=True) for m in src_msgs],
        )
        source_context_files = app.state.context_files.get(sid, {})
        if source_context_files:
            app.state.context_files[new_sess.id] = {
                key: dict(row) for key, row in source_context_files.items()
            }
        app.state.sessions.update(new_sess.id, message_count=len(src_msgs))
        return JSONResponse(
            status_code=201,
            content=Session(**new_sess.to_wire()).model_dump(exclude_none=True),
        )

    # ---- /v1/sessions/{sid}/compact ----------------------------------

    @app.post("/v1/sessions/{sid}/compact")
    async def compact_session(sid: str, request: Request) -> dict[str, Any]:
        sess = app.state.sessions.get(sid)
        if sess is None:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="not_found",
                        message=f"session not found: {sid}",
                        recoverable=True,
                    )
                ).model_dump(exclude_none=True),
            )
        ledger = app.state.messages.get(sid, [])
        if not ledger:
            return {
                "session_id": sid,
                "compacted": False,
                "reason": "session has no messages to compact",
            }

        # Build a transcript blob from the full ledger parts — no deterministic
        # truncation; the LLM downstream is what compacts.
        # ledger entries are Pydantic Message models (see types.py); use
        # attribute access + model_dump() defensively for dict-shaped
        # entries the older code paths still produce.
        def _attr(o, name, default=None):
            if hasattr(o, name):
                return getattr(o, name)
            if isinstance(o, dict):
                return o.get(name, default)
            return default

        # Pass the FULL transcript through to the LLM — clio must not heuristically
        # truncate content a model sees; the LLM is what compacts (that is allowed).
        chunks: list[str] = []
        for m in ledger[-50:]:  # last 50 messages should be enough context
            role = (_attr(m, "role", "user") or "user").upper()
            for p in _attr(m, "parts", []) or []:
                txt = (_attr(p, "text", "") or "").strip()
                if not txt:
                    continue
                chunks.append(f"{role}: {txt}")
        transcript = "\n".join(chunks)
        if not transcript.strip():
            return {
                "session_id": sid,
                "compacted": False,
                "reason": "transcript is empty after part filtering",
            }

        agent = app.state.agent
        if agent is None:
            raise HTTPException(
                status_code=503,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="agent_unavailable",
                        message="no LM agent wired; configure one via PUT /v1/providers/lm",
                        recoverable=True,
                    )
                ).model_dump(exclude_none=True),
            )

        # Try to extract optional focus instructions from the body.
        body = await json_body(request, route="POST /v1/sessions/{sid}/compact")
        focus = (body.get("focus") or "").strip()

        prompt = (
            "Create an evidence-preserving compact memory for the following CLIO "
            "conversation transcript. This memory will replace the archived transcript, "
            "so preserve concrete scientific evidence, not just a high-level story.\n\n"
            "Rules:\n"
            "- Keep exact file paths, dataset names, column names, variable names, "
            "units, dimensions, counts, statistics, artifact paths, and error messages "
            "when they appear in the transcript.\n"
            "- Preserve which findings came from which source, grouped by file/provider "
            "or workflow stage.\n"
            "- Preserve unresolved gaps, failed inspections, missing dependencies, and "
            "next checks.\n"
            "- If evidence is missing or a source was not inspected, say that explicitly. "
            "Do not fill gaps with plausible details.\n"
            "- Do not invent dataset names, columns, statistics, compression settings, "
            "or readiness conclusions that are not supported by the transcript.\n"
            "- Prefer concise structured bullets over prose. Keep the summary compact, "
            "but do not omit identifiers needed for a later expert to continue the work."
        )
        if focus:
            prompt += f"\n\nFocus the summary on: {focus}"
        prompt += f"\n\n--- transcript ---\n{transcript}\n--- end ---"

        def _summarize_with_provider_retries() -> str:
            def summarize() -> str:
                return agent._run_chat_agent(prompt, "")

            retry_call = getattr(agent, "_call_with_transient_provider_retries", None)
            if callable(retry_call):
                return retry_call("compact_summary", summarize)
            return summarize()

        try:
            summary = await asyncio.get_running_loop().run_in_executor(
                None,
                _summarize_with_provider_retries,
            )
            evidence_index = deps.compact_exact_evidence_index(transcript)
            if evidence_index:
                summary = (summary or "").rstrip() + "\n\n" + evidence_index
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(
                status_code=502,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="upstream_error",
                        message=f"compact summarisation failed: {exc!r}",
                        recoverable=True,
                    )
                ).model_dump(exclude_none=True),
            ) from exc

        # Insert the summary as a new assistant message at the head of the
        # ledger (after archiving the originals to a parallel list so a
        # future /resume can recover full history). The TUI doesn't see
        # archived messages — only the compact summary + anything that
        # comes after it.
        event_id = _new_memory_event_id()
        compacted_at = datetime.now(timezone.utc).isoformat()
        archive = app.state.__dict__.setdefault("session_archives", {})
        archive.setdefault(sid, []).append(
            {
                "compacted_at": time.time(),
                "memory_event_id": event_id,
                "messages": list(ledger),
            }
        )

        arc = getattr(agent, "arc", None)
        arc_status = "not_configured"
        if arc is not None:
            try:
                from clio_agent.arc.schema import (  # noqa: PLC0415
                    Conversation as ARCConversation,
                )
                from clio_agent.arc.schema import Message as ARCMessage  # noqa: PLC0415

                now_ts = time.time()
                arc_summary = ARCMessage(
                    role="assistant",
                    content="[compact summary]\n" + (summary or "").strip(),
                    timestamp=now_ts,
                    metadata={
                        "source": "gact_compact",
                        "synthetic": "compact_summary",
                        "memory_event_id": event_id,
                        "archived_count": len(ledger),
                    },
                )
                conv = arc.get_conversation(sid)
                if conv is None:
                    conv = ARCConversation(
                        session_id=sid,
                        user_id="default_user",
                        created_at=now_ts,
                        updated_at=now_ts,
                        last_accessed=now_ts,
                        status="active",
                        messages=[arc_summary],
                        routing_decisions=[],
                        metadata={
                            "clio_agent_version": _installed_clio_agent_version(),
                            "arc_enabled": True,
                            "compacted_by": "gact",
                        },
                        storage_tier="warm",
                    )
                else:
                    conv.messages = [arc_summary]
                    conv.updated_at = now_ts
                    conv.last_accessed = now_ts
                    conv.metadata["compacted_by"] = "gact"
                    conv.metadata["compacted_at"] = now_ts
                    conv.metadata["archived_message_count"] = len(ledger)
                arc.store_conversation(conv)
                arc_status = "stored"
            except Exception as exc:  # noqa: BLE001
                raise HTTPException(
                    status_code=500,
                    detail=ErrorEnvelope(
                        error=ErrorInfo(
                            error="memory_update_failed",
                            message=f"compact summary could not be stored in ARC memory: {exc!r}",
                            recoverable=True,
                        )
                    ).model_dump(exclude_none=True),
                ) from exc

        compact_message = build_compact_summary_message(
            session_id=sid,
            turn_id=_active_semantic_turn_id(),
            summary=summary or "",
            event_id=event_id,
            compacted_message_ids=[
                mid for m in ledger if (mid := _attr(m, "id", ""))
            ],
        )
        deps.replace_session_messages(app, sid, [compact_message])
        memory_event = {
            "id": event_id,
            "version": 1,
            "type": "compact_summary",
            "session_id": sid,
            "created_at": compacted_at,
            "updated_at": compacted_at,
            "summary_message_id": compact_message.id,
            "archived_count": len(ledger),
            "summary_chars": len((summary or "")),
            "transcript_chars": len(transcript),
            "focus": focus,
            "arc_status": arc_status,
            "metadata": {
                "source": "gact_compact",
                "synthetic": "compact_summary",
                "evidence_index": "[exact retained evidence index]" in (summary or ""),
            },
        }
        app.state.memory_events.setdefault(sid, []).append(memory_event)
        _emit_semantic_event(
            app,
            sid,
            "memory.compacted",
            turn_id=_ctx.active_turn_id(),
            trace_id=_ctx.active_trace_id(),
            summary="Session transcript was compacted into memory.",
            actor={"role": "runtime", "component": "memory"},
            subject={"memory_event_id": event_id},
            payload=memory_event,
        )

        # Publish so any open SSE stream redraws.
        app.state.bus.publish(
            Event(
                type="session.compacted",
                session_id=sid,
                payload={
                    "event_id": event_id,
                    "archived_count": len(ledger),
                    "summary_chars": len((summary or "")),
                    "summary_message_id": compact_message.id,
                    "version": 1,
                },
            )
        )
        return {
            "session_id": sid,
            "compacted": True,
            "event_id": event_id,
            "archived_count": len(ledger),
            "summary": summary,
        }

    # ---- /v1/sessions/{sid}/export + /v1/sessions/import (#16) -------

    @app.get("/v1/sessions/{sid}/export")
    async def export_session(sid: str) -> dict[str, Any]:
        """SPEC §6.x — dump a session + its messages as a single
        portable JSON blob. Useful for sharing analyses, archiving,
        replay. Round-trips through POST /v1/sessions/import.
        """

        sess = app.state.sessions.get(sid)
        if sess is None:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="not_found",
                        message=f"session not found: {sid}",
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )
        msgs = app.state.messages.get(sid, [])
        ws = app.state.workspaces.get(sess.workspace_id)
        return {
            "version": "1",
            "session": Session(**sess.to_wire()).model_dump(exclude_none=True),
            "workspace": (Workspace(**ws.to_wire()).model_dump(exclude_none=True) if ws else None),
            "messages": [m.to_wire() for m in msgs],  # #731: slim, arrival-ordered parts
            "context_files": [dict(row) for row in app.state.context_files.get(sid, {}).values()],
        }

    @app.post("/v1/sessions/import", response_model=Session)
    async def import_session(blob: dict[str, Any]) -> Session:
        """Restore a session from an export blob. Creates a fresh
        session in ws_default (or the workspace named in the blob
        if it exists locally) and re-plays the messages as already-
        settled rows. Returns the new Session row.
        """

        sess_data = blob.get("session", {})
        title = sess_data.get("title") or "imported"
        wid = "ws_default"
        if blob.get("workspace") and app.state.workspaces.get(blob["workspace"].get("id", "")):
            wid = blob["workspace"]["id"]
        new_sess = app.state.sessions.create(
            workspace_id=wid,
            title=title,
            metadata=sess_data.get("metadata") or {},
        )
        msg_rows: list[Message] = []
        for m in blob.get("messages", []):
            try:
                msg = Message(**{**m, "session_id": new_sess.id})
                msg_rows.append(msg)
            except Exception:  # noqa: BLE001 - malformed message row skipped during rewind copy
                continue
        deps.replace_session_messages(app, new_sess.id, msg_rows)
        context_files: dict[str, dict[str, Any]] = {}
        for row in blob.get("context_files", []):
            if not isinstance(row, Mapping):
                continue
            path = str(row.get("path") or "").strip()
            if not path:
                continue
            context_files[path] = dict(row)
        if context_files:
            app.state.context_files[new_sess.id] = context_files
        cost_total = sum(float(m.get("cost_usd", 0.0) or 0.0) for m in blob.get("messages", []))
        in_total = sum(
            int((m.get("tokens") or {}).get("input", 0) or 0) for m in blob.get("messages", [])
        )
        out_total = sum(
            int((m.get("tokens") or {}).get("output", 0) or 0) for m in blob.get("messages", [])
        )
        app.state.sessions.update(
            new_sess.id,
            message_count=len(msg_rows),
            add_tokens_input=in_total,
            add_tokens_output=out_total,
            add_cost_usd=cost_total,
        )
        refreshed = app.state.sessions.get(new_sess.id)
        return Session(**refreshed.to_wire())

    # ---- Ask-user and retry protocol (#333) --------------------------

    def _question_not_found(sid: str, question_id: str) -> HTTPException:
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

    def _pending_user_questions(sid: str) -> list[UserQuestion]:
        return [
            q
            for q in app.state.user_questions.values()
            if q.session_id == sid and q.status == "pending"
        ]

    def _set_session_status(
        sid: str,
        status: str,
        *,
        prev_status: str = "",
        metadata_patch: Optional[dict[str, Any]] = None,
    ) -> None:
        updated = app.state.sessions.update(
            sid,
            status=status,
            metadata_patch=metadata_patch,
        )
        app.state.bus.publish(
            Event(
                type="session.status_changed",
                session_id=sid,
                payload={
                    "session_id": sid,
                    "status": status,
                    "prev_status": prev_status,
                    "updated_at": updated.updated_at if updated is not None else "",
                },
            )
        )

    def _normalize_question_options(
        req: CreateUserQuestionRequest,
    ) -> list[UserQuestionOption]:
        if req.kind == "confirmation" and not req.options:
            return [
                UserQuestionOption(label="Yes", value="yes", description=""),
                UserQuestionOption(label="No", value="no", description=""),
            ]
        return list(req.options)

    def _message_text(message: Message) -> str:
        return "\n".join(
            part.text for part in message.parts if part.type == "text" and part.text
        ).strip()

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

    def _retry_user_text(original_text: str, notes: str) -> str:
        notes = notes.strip()
        if not notes:
            return original_text
        return f"{original_text}\n\n[Retry notes]\n{notes}"

    @app.get("/v1/sessions/{sid}/questions")
    async def list_user_questions(sid: str, status: str = "") -> dict[str, Any]:
        if app.state.sessions.get(sid) is None:
            raise _session_not_found(sid)
        rows = [q for q in app.state.user_questions.values() if q.session_id == sid]
        if status:
            rows = [q for q in rows if q.status == status]
        rows.sort(key=lambda q: q.created_at, reverse=True)
        return {"questions": [q.model_dump(exclude_none=True) for q in rows]}

    @app.post("/v1/sessions/{sid}/questions", response_model=UserQuestion, status_code=201)
    async def create_user_question(
        sid: str,
        req: CreateUserQuestionRequest,
    ) -> UserQuestion:
        sess = app.state.sessions.get(sid)
        if sess is None:
            raise _session_not_found(sid)
        prompt = req.prompt.strip()
        if not prompt:
            raise HTTPException(
                status_code=422,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="bad_request",
                        message="missing required field: prompt",
                        details={"field": "prompt"},
                        recoverable=True,
                    )
                ).model_dump(exclude_none=True),
            )
        now_iso = datetime.now(timezone.utc).isoformat()
        row = UserQuestion(
            id=_new_question_id(),
            session_id=sid,
            prompt=prompt,
            kind=req.kind,
            options=_normalize_question_options(req),
            created_at=now_iso,
            updated_at=now_iso,
            expires_at=req.expires_at,
            source=req.source or "orchestrator",
            turn_id=req.turn_id,
            attempt_id=req.attempt_id,
            metadata=req.metadata,
        )
        app.state.user_questions[row.id] = row
        _set_session_status(
            sid,
            "waiting_user",
            prev_status=sess.status,
            metadata_patch={"pending_user_question_id": row.id},
        )
        app.state.bus.publish(
            Event(
                type="user_question.created",
                session_id=sid,
                payload=row.model_dump(exclude_none=True),
            )
        )
        return row

    @app.post("/v1/sessions/{sid}/questions/{question_id}/answer", response_model=UserQuestion)
    async def answer_user_question(
        sid: str,
        question_id: str,
        req: AnswerUserQuestionRequest,
    ) -> UserQuestion:
        if app.state.sessions.get(sid) is None:
            raise _session_not_found(sid)
        row = app.state.user_questions.get(question_id)
        if row is None or row.session_id != sid:
            raise _question_not_found(sid, question_id)
        if row.status != "pending":
            raise HTTPException(
                status_code=409,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="bad_request",
                        message=f"user question is already {row.status}",
                        details={"session_id": sid, "question_id": question_id},
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )
        allowed_values = {o.value or o.label for o in row.options}
        selected = [s for s in req.selected_options if s]
        if allowed_values and selected and any(s not in allowed_values for s in selected):
            raise HTTPException(
                status_code=422,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="bad_request",
                        message="selected option is not valid for this question",
                        details={
                            "session_id": sid,
                            "question_id": question_id,
                            "allowed": sorted(allowed_values),
                        },
                        recoverable=True,
                    )
                ).model_dump(exclude_none=True),
            )
        updated = row.model_copy(
            update={
                "status": "answered",
                "answer": req.answer,
                "selected_options": selected,
                "answer_metadata": req.metadata,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        app.state.user_questions[question_id] = updated
        if not _pending_user_questions(sid):
            sess = app.state.sessions.get(sid)
            should_resume = bool(updated.metadata.get("resume_on_answer")) and sess is not None
            # #948 S1: the within-session busy gate covers the RESUME producer too.
            # If a turn is already in flight (an intervening POST staged one while
            # this question was pending), staging the resume now would overwrite the
            # running turn's slot and orphan it. DEFER it (never drop — losing the
            # user's answer would be a silent-fallback bug): record the prepared
            # resume; the turn-runner idle hook (_redrive_deferred_resume) re-drives
            # it the instant the session frees. A typed event reaches the trace/API.
            if should_resume and app.state.agent is not None and app.state.turn_runner.busy(sid):
                app.state.deferred_resumes[sid] = {
                    "text": deps.ask_user_resume_text(updated),
                    "metadata": {
                        "ask_user_question_id": updated.id,
                        "ask_user_prompt": updated.prompt,
                        "ask_user_answer": updated.answer,
                        "ask_user_selected_options": updated.selected_options,
                        "ask_user_source_turn_id": updated.turn_id,
                        "ask_user_attempt_id": updated.attempt_id,
                        "ask_user_caller": updated.metadata.get("caller", {}),
                        "ask_user_resume": True,
                    },
                    "question_id": updated.id,
                }
                app.state.sessions.update(sid, metadata_patch={"pending_user_question_id": ""})
                app.state.bus.publish(
                    Event(
                        type="user_question.resume_deferred",
                        session_id=sid,
                        payload={
                            "question_id": updated.id,
                            "session_id": sid,
                            "reason": "session_busy",
                        },
                    )
                )
                logger.info(
                    "user_question resume deferred reason=session_busy "
                    "session_id=%s question_id=%s",
                    sid,
                    question_id,
                )
            elif should_resume and app.state.agent is not None:
                app.state.sessions.update(
                    sid,
                    metadata_patch={"pending_user_question_id": ""},
                )
                resumed_msg = deps.start_background_user_turn(
                    sid,
                    sess,
                    deps.ask_user_resume_text(updated),
                    metadata={
                        "ask_user_question_id": updated.id,
                        "ask_user_prompt": updated.prompt,
                        "ask_user_answer": updated.answer,
                        "ask_user_selected_options": updated.selected_options,
                        "ask_user_source_turn_id": updated.turn_id,
                        "ask_user_attempt_id": updated.attempt_id,
                        "ask_user_caller": updated.metadata.get("caller", {}),
                        "ask_user_resume": True,
                    },
                    prev_status=sess.status if sess is not None else "waiting_user",
                )
                app.state.bus.publish(
                    Event(
                        type="user_question.resumed",
                        session_id=sid,
                        payload={
                            "question_id": updated.id,
                            "session_id": sid,
                            "queued_user_message_id": resumed_msg.id,
                            "source_turn_id": updated.turn_id,
                        },
                    )
                )
            else:
                _set_session_status(
                    sid,
                    "idle",
                    prev_status=sess.status if sess is not None else "waiting_user",
                    metadata_patch={"pending_user_question_id": ""},
                )
        app.state.bus.publish(
            Event(
                type="user_question.answered",
                session_id=sid,
                payload=updated.model_dump(exclude_none=True),
            )
        )
        return updated

    @app.post("/v1/sessions/{sid}/questions/{question_id}/cancel", response_model=UserQuestion)
    async def cancel_user_question(sid: str, question_id: str) -> UserQuestion:
        if app.state.sessions.get(sid) is None:
            raise _session_not_found(sid)
        row = app.state.user_questions.get(question_id)
        if row is None or row.session_id != sid:
            raise _question_not_found(sid, question_id)
        if row.status == "pending":
            row = row.model_copy(
                update={
                    "status": "cancelled",
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }
            )
            app.state.user_questions[question_id] = row
        if not _pending_user_questions(sid):
            sess = app.state.sessions.get(sid)
            _set_session_status(
                sid,
                "idle",
                prev_status=sess.status if sess is not None else "waiting_user",
                metadata_patch={"pending_user_question_id": ""},
            )
        app.state.bus.publish(
            Event(
                type="user_question.cancelled",
                session_id=sid,
                payload=row.model_dump(exclude_none=True),
            )
        )
        return row

    @app.get("/v1/sessions/{sid}/attempts")
    async def list_turn_attempts(sid: str) -> dict[str, Any]:
        if app.state.sessions.get(sid) is None:
            raise _session_not_found(sid)
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
            raise _session_not_found(sid)
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
                                "recovery_actions": [
                                    "wait",
                                    "check_lm_provider_status",
                                    "retry",
                                ],
                            },
                            recoverable=True,
                        )
                    ).model_dump(exclude_none=True),
                )
            if source_user is None or not _message_text(source_user):
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
        # #948 S1: the within-session busy gate covers the retry producer too. A
        # retry-execute while a turn is already in flight would double-stage a
        # concurrent turn on this session (orphaning the running one, both writing
        # the same session + ARC). Record the attempt with a typed blocked reason
        # instead of staging; the client retries once the running turn finishes.
        if req.execute and not execution_blocked_reason and app.state.turn_runner.busy(sid):
            execution_blocked_reason = "session_busy"
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
            retry_text = _retry_user_text(_message_text(source_user), req.notes)
            retry_user_msg = deps.start_background_user_turn(
                sid,
                sess,
                retry_text,
                metadata={
                    "retry_attempt_id": attempt.id,
                    "retry_source_message_id": message_id,
                    "retry_source_user_message_id": source_user.id,
                    "retry_notes": req.notes,
                    **req.metadata,
                },
                prev_status=sess.status,
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

    # ---- POST /v1/sessions/{sid}/cancel (BBB20) -----------------------

    @app.post("/v1/sessions/{sid}/cancel")
    async def cancel_session(sid: str) -> Response:
        """Best-effort cancel of an in-flight turn on this session.

        The agent loop and sync MCP bridge observe a scoped cancellation
        checker between planner/expert/tool boundaries and return early
        with ``error_info.error == "cancelled"`` when possible. The
        endpoint itself flips the flag + publishes a
        ``session.cancelled`` event so any live SSE subscriber sees
        the transition without waiting for the next turn boundary.

        If the turn is already blocked inside executor-thread provider
        or tool work, cancelling the asyncio Task settles the GACT
        envelope as cancelled but cannot kill the underlying Python
        thread. The emitted status event marks this as best-effort so
        clients do not mistake it for a guaranteed provider abort.

        Returns 204 whether a turn was actually running — the TUI
        fires this on Esc/Ctrl+C speculatively and doesn't want an
        error if the race finished on its own.
        """

        sess = app.state.sessions.get(sid)
        if sess is None:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="not_found",
                        message=f"session not found: {sid}",
                        details={"session_id": sid},
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )

        # Set the cancellation flag. Cooperative agent/tool paths check
        # it between expensive boundaries; the turn handler also checks
        # it after forward() returns so non-cooperative agents still
        # produce a truthful cancelled envelope.
        app.state.cancel_flags.add(sid)
        event = app.state.cancel_events.get(sid)
        if event is not None:
            event.set()
        in_flight = app.state.in_flight_turns.get(sid)
        cancellation_pending = False
        if in_flight is not None and not in_flight.done():
            cancellation_pending = True
        attempt = {
            "id": _new_cancellation_attempt_id(),
            "session_id": sid,
            "requested_at": datetime.now(timezone.utc).isoformat(),
            "in_flight": cancellation_pending,
            "cooperative_signal_sent": event is not None,
            "asyncio_task_cancel_scheduled": cancellation_pending,
            "asyncio_task_cancel_sent": False,
            "hard_abort_supported": False,
            "upstream_abort": "not_supported",
            "executor_work_may_continue": cancellation_pending,
        }
        app.state.cancel_attempts[sid] = attempt
        if cancellation_pending:

            async def _cancel_after_grace(task: asyncio.Task, session_id: str) -> None:
                await asyncio.sleep(0.1)
                if session_id in app.state.cancel_flags and not task.done():
                    latest_attempt = app.state.cancel_attempts.get(session_id)
                    if latest_attempt is attempt:
                        attempt["asyncio_task_cancel_sent"] = True
                        attempt["asyncio_task_cancelled_at"] = datetime.now(
                            timezone.utc
                        ).isoformat()
                    task.cancel()

            asyncio.create_task(_cancel_after_grace(in_flight, sid))
        app.state.sessions.update(sid, status="cancelled")
        app.state.bus.publish(
            Event(
                type="session.status_changed",
                session_id=sid,
                payload={
                    "session_id": sid,
                    "status": "cancelled",
                    "prev_status": sess.status,
                    "execution_cancellation": (
                        "cooperative_pending" if cancellation_pending else "none"
                    ),
                    "executor_work_may_continue": cancellation_pending,
                    "cancellation_attempt": deps.cancellation_attempt_summary(attempt),
                },
            )
        )
        return Response(status_code=204)
