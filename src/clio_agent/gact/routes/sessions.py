"""Session lifecycle routes for the GACT server (#714); turn retry lives in turn_retry.

* CRUD creates, lists, patches, and permission-gates session deletion.
* Undo/rewind drop trailing messages and publish their lifecycle events.
* Fork/import/export provide branching and portable JSON transfer.
* Compaction retains the transcript; ``gact.compaction`` shrinks model-facing context.
* Cancel cooperatively stops an in-flight turn and publishes its status.
Shared route dependencies travel on :class:`GactDeps`.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Optional

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from clio_agent.gact.autonomous_loop import stop_session_loop
from clio_agent.gact.compaction import CompactionError, compact_session_context
from clio_agent.gact.context_rollback import follow_rollback
from clio_agent.gact.events import Event
from clio_agent.gact.goal import stop_session_goal
from clio_agent.gact.mcp_apps import cleanup_session_mcp_apps
from clio_agent.gact.message_marks import validated_session_metadata
from clio_agent.gact.message_submission_errors import session_not_found
from clio_agent.gact.off_loop import join_off_loop, run_off_loop
from clio_agent.gact.protocol_v3 import project_for_request, session_to_v3
from clio_agent.gact.routes._body import NonObjectBodyError, json_body
from clio_agent.gact.routes.dashboard_reports import register_dashboard_report_routes
from clio_agent.gact.routes.session_a2ui_preservation import preserve_a2ui, split_preserved_a2ui
from clio_agent.gact.routes.session_cancellation import cancel_session_state_async
from clio_agent.gact.routes.session_creation import register_session_creation_route
from clio_agent.gact.routes.session_export import register_session_export_routes
from clio_agent.gact.routes.session_fork import register_session_fork_route
from clio_agent.gact.routes.session_list import register_session_list_route
from clio_agent.gact.routes.side_sessions import register_side_session_routes
from clio_agent.gact.routes.turn_retry import register_turn_retry_routes
from clio_agent.gact.session_processes import close_session_processes
from clio_agent.gact.session_tool_output import delete_session_tool_output
from clio_agent.gact.turn_reasoning import forget_session_reasoning
from clio_agent.gact.types import (
    ErrorEnvelope,
    ErrorInfo,
    Message,
    Session,
    UpdateSessionRequest,
)
from clio_agent.gact.usage import reported_cost_total
from clio_agent.lm.session_clients import close_session_model_clients

if TYPE_CHECKING:
    from clio_agent.gact.routes.deps import GactDeps

logger = logging.getLogger(__name__)


def register_sessions_routes(app: FastAPI, deps: "GactDeps") -> None:
    """Register the ``/v1/sessions`` lifecycle routes (and turn retry) on ``app``.

    Handlers close over the ``app`` argument (FastAPI's decorators need it) and
    reach sessions/messages/questions/attempts state + the live event bus through
    ``app.state``. Cross-concern ``build_app`` helpers (ledger replace,
    context-file/ARC release, model-ref errors, the evidence index, the
    ask-user resume text, the destructive-action guard, and the background-turn
    entrypoint) travel through ``deps`` rather than importing back into
    ``gact.app``. The rollback + ask-user/retry helper closures are concern-private.
    """

    from clio_agent.gact.fleet.spotter_model import (  # noqa: PLC0415
        SPOTTER_MODEL_KEY,
        rearm_on_model_change,
    )
    from clio_agent.gact.spotter_watcher import sync_watcher_for_mode  # noqa: PLC0415

    register_session_creation_route(app)

    @app.patch("/v1/sessions/{sid}", response_model=Session)
    async def patch_session(
        sid: str, req: UpdateSessionRequest, request: Request
    ) -> Session | JSONResponse:
        """Update session settings and durable user navigation metadata."""

        prior = app.state.sessions.get(sid)
        prior_mode = str(getattr(prior, "approval_mode", "") or "")
        prior_spotter_model = (getattr(prior, "metadata", None) or {}).get(SPOTTER_MODEL_KEY)
        sess = app.state.sessions.update(
            sid,
            title=req.title,
            mode=req.mode,
            edit_mode=req.edit_mode,
            routing_mode=req.routing_mode,
            approval_mode=req.approval_mode,
            model=req.model.model_dump(exclude_none=True) if req.model else None,
            agent=req.agent.model_dump(exclude_none=True) if req.agent else None,
            # iowarp/gact-tui §audit/E-14: persist pin + archive state.
            metadata_patch=validated_session_metadata(app, sid, req.metadata),
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
        await join_off_loop(sync_watcher_for_mode, app, sess, prior_approval_mode=prior_mode)
        await join_off_loop(
            rearm_on_model_change,
            app,
            sess,
            prior_approval_mode=prior_mode,
            prior_spotter_model=prior_spotter_model,
        )
        # Publish so live SSE subscribers see mode flips immediately.
        app.state.bus.publish(
            Event(
                type="session.updated",
                session_id=sid,
                payload=Session(**sess.to_wire()).model_dump(exclude_none=True),
            )
        )
        return project_for_request(
            request,
            v3=lambda: JSONResponse(content=session_to_v3(sess)),
            v2=lambda: Session(**sess.to_wire()),
        )

    register_session_list_route(app)

    @app.get("/v1/sessions/{sid}", response_model=Session)
    async def get_session(
        request: Request, sid: str, workspace_id: Optional[str] = None
    ) -> Session | JSONResponse:
        sess = app.state.sessions.get(sid)
        if sess is None:
            raise session_not_found(sid)
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
        return project_for_request(
            request,
            v3=lambda: JSONResponse(content=session_to_v3(sess)),
            v2=lambda: Session(**sess.to_wire()),
        )

    @app.delete("/v1/sessions/{sid}")
    async def delete_session(sid: str) -> Response:
        sess = app.state.sessions.get(sid)
        if sess is None:
            raise session_not_found(sid)
        deps.guard_direct_destructive_action(
            app,
            session_id=sid,
            workspace_id=sess.workspace_id,
            tool_name="gact.session.delete",
            args={"session_id": sid},
            summary=f"delete session {sid}",
            reason="user_requested_session_delete",
        )
        active = app.state.task_history.tree_handles(sid, active_only=True)
        if active or app.state.turn_runner.busy(sid):
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "session_has_active_work",
                    "message": "Cancel and settle active work before deleting this session.",
                    "handles": sorted(active),
                },
            )
        # P4.1 #1079 cancel-both: a deleted session must not orphan its loop wakeup.
        stop_session_loop(app, sid)
        stop_session_goal(app, sid)
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
        await run_off_loop(deps.delete_session_messages, app, sid)  # clio-core first: 503 keeps it
        existed = await run_off_loop(app.state.sessions.delete, sid)  # trusted: off-loop only
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
        forget_session_reasoning(app, sid)
        await run_off_loop(close_session_model_clients, app, sid)
        deps.delete_session_context_files(app, sid)
        await run_off_loop(delete_session_tool_output, app, sess.workspace_id, sid)
        await run_off_loop(close_session_processes, app, sid)  # its kernels and pages (BR23)
        await run_off_loop(deps.release_session_arc, app, sid)  # #1334: drops _events scopes
        await run_off_loop(app.state.task_history.restore)
        app.state.a2ui_catalogs.forget_session(sid)
        app.state.a2ui_store.forget_session(sid)
        return Response(status_code=204)

    async def copy_session_context(src: str, dst: str, at: str = "") -> int:
        """Deep-copy ``src``'s messages (to + including ``at``) and context files into ``dst``."""
        from clio_agent.gact.conversation_copy import copy_conversation  # noqa: PLC0415

        return await run_off_loop(copy_conversation, app, src, dst, at)

    register_side_session_routes(
        app, copy_context=copy_session_context, delete_session=delete_session
    )
    register_session_fork_route(app)

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

    async def _commit_rollback(  # #1334: awaited; the ledger replace is store RPCs
        sid: str,
        *,
        operation: str,
        kept_messages: list[Message],
        deleted_messages: list[Message],
        target_message_id: str = "",
        include_target: bool = False,
    ) -> dict[str, Any]:
        from clio_agent.gact.session_input import withdraw_message_inputs

        await run_off_loop(withdraw_message_inputs, app, sid, [m.id for m in deleted_messages])
        replacement_messages = preserve_a2ui(sid, kept_messages, deleted_messages, operation)
        await run_off_loop(deps.replace_session_messages, app, sid, replacement_messages)
        # The agents' context follows the ledger: rolled-back turns leave it too.
        await run_off_loop(follow_rollback, app, sid, deleted_messages, replacement_messages)
        deleted_ids = [m.id for m in deleted_messages]
        updated = app.state.sessions.update(
            sid,
            message_count=len(replacement_messages),
            status="idle",
            metadata_patch={
                "message_marks": dict.fromkeys(deleted_ids, False),
                "last_rollback": {
                    "operation": operation,
                    "deleted_message_ids": deleted_ids,
                    "target_message_id": target_message_id,
                    "include_target": include_target,
                    "memory_scope": "gact_visible_transcript_only",
                    "occurred_at": datetime.now(timezone.utc).isoformat(),
                },
            },
        )
        if updated is None:
            raise session_not_found(sid)
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
            "message_count": len(replacement_messages),
            "memory_scope": "gact_visible_transcript_only",
            "session": session_payload,
        }

    @app.post("/v1/sessions/{sid}/undo")
    async def undo_session(sid: str, request: Request) -> dict[str, Any]:
        sess = app.state.sessions.get(sid)
        if sess is None:
            raise session_not_found(sid)
        _reject_rollback_while_active(sid, sess)
        # Malformed/``null`` body is ``{}`` (traced); a non-object JSON payload is a
        # 422 -- undo is destructive and must not proceed on a coerced body.
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
        messages, carried = split_preserved_a2ui(list(app.state.messages.get(sid, [])))
        deleted = messages[-count:]
        kept = [*messages[: max(0, len(messages) - count)], *carried]
        deps.guard_direct_destructive_action(
            app,
            session_id=sid,
            workspace_id=sess.workspace_id,
            tool_name="gact.session.undo",
            args={"session_id": sid, "count": count},
            summary=f"undo last {count} message(s) in session {sid}",
            reason="user_requested_session_undo",
        )
        return await _commit_rollback(
            sid,
            operation="undo",
            kept_messages=kept,
            deleted_messages=deleted,
        )

    @app.post("/v1/sessions/{sid}/rewind")
    async def rewind_session(sid: str, request: Request) -> dict[str, Any]:
        sess = app.state.sessions.get(sid)
        if sess is None:
            raise session_not_found(sid)
        _reject_rollback_while_active(sid, sess)
        # A malformed body is ``{}`` (traced); a non-object JSON payload, ``null``
        # included, is a 422 -- rewind is destructive and must not proceed on a
        # wrong-shaped body coerced to defaults.
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
        return await _commit_rollback(
            sid,
            operation="rewind",
            kept_messages=kept,
            deleted_messages=deleted,
            target_message_id=target_message_id,
            include_target=include_target,
        )

    # ---- /v1/sessions/{sid}/compact ----------------------------------

    @app.post("/v1/sessions/{sid}/compact")
    async def compact_session(sid: str, request: Request, scope: str = "") -> dict[str, Any]:
        """Compact the session's context NOW: the manual trigger (the panel too, with
        ``?scope=``) of the ONE operation, :func:`~clio_agent.gact.compaction.
        compact_session_context`; no ``scope`` compacts every agent scope."""
        body = await json_body(request, route="POST /v1/sessions/{sid}/compact")
        focus = (body.get("focus") or "").strip()
        compact = compact_session_context
        try:
            return await run_off_loop(
                lambda: compact(app, sid, trigger="manual", focus=focus, scope=scope)
            )
        except CompactionError as exc:
            raise HTTPException(status_code=exc.status, detail=exc.envelope()) from exc

    # ---- /v1/sessions/{sid}/export + /v1/sessions/import (#16) -------

    register_session_export_routes(app)
    register_dashboard_report_routes(app)

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
        await run_off_loop(deps.replace_session_messages, app, new_sess.id, msg_rows)
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
            add_cost_usd=reported_cost_total(blob.get("messages", [])),
        )
        refreshed = app.state.sessions.get(new_sess.id)
        return Session(**refreshed.to_wire())

    register_turn_retry_routes(app, deps)

    # ---- POST /v1/sessions/{sid}/cancel (BBB20) -----------------------

    @app.post("/v1/sessions/{sid}/cancel")
    async def cancel_session(sid: str) -> Response:
        """Request Stop; keep the session reserved until its worker actually retires.

        Accepted background work continues. Shared task cancellation is separate.
        Return 204 even when the session is already idle.
        """
        await cancel_session_state_async(app, deps, sid)
        return Response(status_code=204)
