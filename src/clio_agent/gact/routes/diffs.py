"""File-diff + context-file routes for the GACT server (#714).

This concern owns two adjacent, session-scoped working-set surfaces the
gact-tui renders side by side:

* **File diffs** -- the pending/applied unified diffs an assistant turn
  proposed (via the ``propose_edit`` tool). The TUI lists them, then the user
  explicitly applies or rejects them:

  - ``GET  /v1/sessions/{sid}/diffs`` -- every pending/applied diff row.
  - ``GET  /v1/sessions/{sid}/messages/{message_id}/diffs`` -- the diffs a
    specific assistant message produced.
  - ``POST /v1/sessions/{sid}/diffs/apply`` -- mark selected (or all) pending
    diffs applied *and* commit them to disk through the workspace + permission
    boundary; per-path write failures land in ``write_errors`` and a
    ``file.diff.write_failed`` event without blocking the rest.
  - ``POST /v1/sessions/{sid}/diffs/reject`` -- mark selected (or all) pending
    diffs rejected and publish ``file.diff.rejected`` events.

* **Context files** -- files the user pins into a session's context, plus the
  per-turn context truth frames CLIO records:

  - ``GET    /v1/sessions/{sid}/context/frames`` / ``.../frames/{frame_id}`` --
    list / fetch the per-turn context frames.
  - ``GET    /v1/sessions/{sid}/context/files`` -- the attached-file ledger.
  - ``POST   /v1/sessions/{sid}/context/files`` -- attach (upsert) a file in
    ``read`` / ``pin`` / ``edit`` mode, validating the path against the
    workspace boundary.
  - ``DELETE /v1/sessions/{sid}/context/files`` -- detach a file (idempotent;
    gated by the destructive-action guard when a row actually matches).

Handlers reach ``app.state`` directly for the live ledgers
(``pending_diffs`` / ``context_files`` / ``context_frames`` / ``sessions`` /
``workspaces`` / ``bus``). Two genuinely cross-concern, permission-bearing
seams travel on :class:`~clio_agent.gact.routes.deps.GactDeps`: the
diff-to-disk commit (``apply_edit_to_disk``, which enforces the workspace +
file-policy boundary and records an audit row) and the destructive-action
guard for context-file deletes. The context-file ledger flush is also threaded
through ``deps`` because it has a second owner (session deletion) in
:mod:`clio_agent.gact.app`. The wire-shaping + path-resolution + path-filter
helpers are concern-private and live here. This module imports only leaf
packages and never loads :mod:`clio_agent.gact.app`.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI, HTTPException, Request, Response

from clio_agent.gact.events import Event
from clio_agent.gact.runtime.retention import enforce_list_bound
from clio_agent.gact.types import ErrorEnvelope, ErrorInfo

if TYPE_CHECKING:
    from clio_agent.gact.routes.deps import GactDeps


def register_diffs_routes(app: FastAPI, deps: "GactDeps") -> None:
    """Register the file-diff + context-file routes on ``app``.

    Handlers close over the ``app`` argument (FastAPI's decorators need it) and
    reach the live ledgers through ``app.state``. The permission-bearing
    cross-concern seams (``deps.apply_edit_to_disk`` for the diff-to-disk
    commit, ``deps.guard_direct_destructive_action`` for context-file delete)
    and the second-owner ledger flush (``deps.flush_context_files``) travel on
    ``deps``; the wire/path/filter helpers below are concern-private closures.
    """

    def _filter_diff_paths(
        rows: list[dict[str, Any]], paths: list[str]
    ) -> list[dict[str, Any]]:
        """Narrow pending diffs to a given path allow-list. Empty
        list (or no param) means "every pending row"."""

        if not paths:
            return [r for r in rows if r["status"] == "pending"]
        allow = set(paths)
        return [r for r in rows if r["path"] in allow and r["status"] == "pending"]

    def _diff_row_to_wire(row: dict[str, Any]) -> dict[str, Any]:
        """Convert an internal pending-diff row to the GACT file_diff shape."""

        status = str(row.get("status") or "pending")
        out: dict[str, Any] = {
            "path": row.get("path", ""),
            "applied": status == "applied",
            "status": status,
        }
        if row.get("unified_diff") is not None:
            out["unified_diff"] = row.get("unified_diff")
        if row.get("part_id"):
            out["part_id"] = row.get("part_id")
        if row.get("message_id"):
            out["message_id"] = row.get("message_id")
        return out

    @app.get("/v1/sessions/{sid}/diffs")
    async def list_session_diffs(sid: str) -> dict[str, Any]:
        """SPEC §6.6/§6.9 read endpoint for pending/applied file diffs."""

        sess = app.state.sessions.get(sid)
        if sess is None:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="internal_error",
                        message=f"session not found: {sid}",
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )
        return {"diffs": [_diff_row_to_wire(row) for row in app.state.pending_diffs.get(sid, [])]}

    @app.get("/v1/sessions/{sid}/messages/{message_id}/diffs")
    async def list_message_diffs(sid: str, message_id: str) -> dict[str, Any]:
        """Return file diffs associated with a specific assistant message."""

        sess = app.state.sessions.get(sid)
        if sess is None:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="internal_error",
                        message=f"session not found: {sid}",
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )
        if not any(m.id == message_id for m in app.state.messages.get(sid, [])):
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="not_found",
                        message=f"message not found: {message_id}",
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )
        return {
            "diffs": [
                _diff_row_to_wire(row)
                for row in app.state.pending_diffs.get(sid, [])
                if row.get("message_id") == message_id
            ]
        }

    @app.post("/v1/sessions/{sid}/diffs/apply")
    async def diffs_apply(sid: str, request: Request) -> dict[str, Any]:
        """Mark pending diffs as applied + actually write to disk
        via the fs_apply_edit_write MCP tool.

        Body: ``{paths: [...]}`` (optional). If omitted, every
        pending diff is applied. Returns ``{applied: [...],
        write_errors?: {...}}``. iowarp/clio-agent#4: writes are
        scoped to the session's workspace.root_path; failures
        per-path go into write_errors but don't block the rest.
        """

        sess = app.state.sessions.get(sid)
        if sess is None:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="internal_error",
                        message=f"session not found: {sid}",
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )
        try:
            body = await request.json()
        except Exception:
            body = {}
        if not isinstance(body, dict):
            body = {}
        paths = [p for p in (body.get("paths") or []) if isinstance(p, str)]

        rows = app.state.pending_diffs.get(sid, [])
        targets = _filter_diff_paths(rows, paths)
        applied: list[str] = []
        write_errors: dict[str, str] = {}
        for r in targets:
            # iowarp/clio-agent#4: actually write to disk if the
            # row carries a `new_content` field. The
            # propose_edit-driven path always sets it; legacy/test
            # diffs that don't get the wire event but no write.
            new_content = r.get("new_content")
            if new_content is not None:
                try:
                    deps.apply_edit_to_disk(
                        path=r["path"],
                        new_content=new_content,
                        session=sess,
                        app=app,
                    )
                except Exception as exc:  # noqa: BLE001
                    err = repr(exc)
                    write_errors[r["path"]] = err
                    r["status"] = "apply_failed"
                    # Publish a failure event so the TUI sees the write
                    # error live (was a silent failure: the response
                    # body carried write_errors but the TUI's apply-
                    # button path discards it). file.diff.write_failed
                    # mirrors file.diff.applied for parity.
                    app.state.bus.publish(
                        Event(
                            type="file.diff.write_failed",
                            session_id=sid,
                            payload={
                                "session_id": sid,
                                "path": r["path"],
                                "part_id": r.get("part_id", ""),
                                "message_id": r.get("message_id", ""),
                                "error": err,
                            },
                        )
                    )
                    continue
            r["status"] = "applied"
            applied.append(r["path"])
            app.state.bus.publish(
                Event(
                    type="file.diff.applied",
                    session_id=sid,
                    payload={
                        "session_id": sid,
                        "path": r["path"],
                        "part_id": r.get("part_id", ""),
                        "message_id": r.get("message_id", ""),
                    },
                )
            )
        # #770 C3: apply flips rows to a terminal status; reclaim now-terminal
        # rows so the per-session bucket does not wait for the next diff append.
        enforce_list_bound(app, rows, "pending_diffs", session_id=sid)
        out: dict[str, Any] = {"applied": applied}
        if write_errors:
            out["write_errors"] = write_errors
        return out

    @app.post("/v1/sessions/{sid}/diffs/reject")
    async def diffs_reject(sid: str, request: Request) -> dict[str, list[str]]:
        """Mark pending diffs as rejected + publish events."""

        sess = app.state.sessions.get(sid)
        if sess is None:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="internal_error",
                        message=f"session not found: {sid}",
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )
        try:
            body = await request.json()
        except Exception:
            body = {}
        if not isinstance(body, dict):
            body = {}
        paths = [p for p in (body.get("paths") or []) if isinstance(p, str)]

        rows = app.state.pending_diffs.get(sid, [])
        targets = _filter_diff_paths(rows, paths)
        rejected: list[str] = []
        for r in targets:
            r["status"] = "rejected"
            rejected.append(r["path"])
            app.state.bus.publish(
                Event(
                    type="file.diff.rejected",
                    session_id=sid,
                    payload={
                        "session_id": sid,
                        "path": r["path"],
                        "part_id": r.get("part_id", ""),
                        "message_id": r.get("message_id", ""),
                    },
                )
            )
        # #770 C3: reject flips rows to a terminal status; reclaim them now.
        enforce_list_bound(app, rows, "pending_diffs", session_id=sid)
        return {"rejected": rejected}

    # ---- /v1/sessions/{sid}/context/files (BBB22) ---------------------

    def _resolve_context_attachment_path(
        *,
        sess: Any,
        raw_path: str,
        requested_workspace_id: str = "",
    ) -> dict[str, Any]:
        source = "mention" if raw_path.startswith("@") else "api"
        attachment_path = raw_path[1:].strip() if raw_path.startswith("@") else raw_path
        if not attachment_path:
            raise HTTPException(
                status_code=422,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="internal_error",
                        message="missing required field: path",
                        recoverable=True,
                    )
                ).model_dump(exclude_none=True),
            )
        workspace_id = requested_workspace_id or getattr(sess, "workspace_id", "") or "ws_default"
        path_obj = Path(attachment_path).expanduser()
        if path_obj.is_absolute():
            try:
                resolved = path_obj.resolve(strict=False)
            except (OSError, ValueError) as exc:
                raise HTTPException(
                    status_code=422,
                    detail=ErrorEnvelope(
                        error=ErrorInfo(
                            error="bad_request",
                            message=f"invalid context file path: {raw_path}",
                            details={"field": "path", "original_error": type(exc).__name__},
                            recoverable=True,
                        )
                    ).model_dump(exclude_none=True),
                ) from exc
            return {
                "path": str(resolved),
                "display_path": attachment_path,
                "resolved_path": str(resolved),
                "workspace_id": workspace_id,
                "source": source,
            }

        ws = app.state.workspaces.get(workspace_id)
        if ws is None:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="not_found",
                        message=f"workspace not found: {workspace_id}",
                        details={"workspace_id": workspace_id},
                        recoverable=True,
                    )
                ).model_dump(exclude_none=True),
            )
        try:
            root = Path(ws.root_path or os.getcwd()).expanduser().resolve()
            resolved = (root / attachment_path).resolve(strict=False)
            resolved.relative_to(root)
        except ValueError:
            raise HTTPException(
                status_code=403,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="path_outside_workspace",
                        message=f"context path escapes workspace: {raw_path}",
                        details={"path": raw_path, "workspace_id": workspace_id},
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            ) from None
        except (OSError, RuntimeError) as exc:
            raise HTTPException(
                status_code=422,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="bad_request",
                        message=f"invalid context file path: {raw_path}",
                        details={"field": "path", "original_error": type(exc).__name__},
                        recoverable=True,
                    )
                ).model_dump(exclude_none=True),
            ) from exc
        display_path = attachment_path.replace("\\", "/")
        return {
            "path": display_path,
            "display_path": display_path,
            "resolved_path": str(resolved),
            "workspace_id": workspace_id,
            "source": source,
        }

    @app.get("/v1/sessions/{sid}/context/frames")
    async def list_context_frames(sid: str, limit: int = 50) -> dict[str, Any]:
        """List per-turn context truth frames for a session."""

        if app.state.sessions.get(sid) is None:
            raise HTTPException(
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
        limit = max(1, min(int(limit or 50), 200))
        rows = list(app.state.context_frames.get(sid, []))
        return {"frames": rows[-limit:]}

    @app.get("/v1/sessions/{sid}/context/frames/{frame_id}")
    async def get_context_frame(sid: str, frame_id: str) -> dict[str, Any]:
        if app.state.sessions.get(sid) is None:
            raise HTTPException(
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
        for row in app.state.context_frames.get(sid, []):
            if row.get("id") == frame_id:
                return {"frame": row}
        raise HTTPException(
            status_code=404,
            detail=ErrorEnvelope(
                error=ErrorInfo(
                    error="not_found",
                    message=f"context frame not found: {frame_id}",
                    details={"session_id": sid, "frame_id": frame_id},
                    recoverable=False,
                )
            ).model_dump(exclude_none=True),
        )

    @app.get("/v1/sessions/{sid}/context/files")
    async def list_context_files(sid: str) -> dict[str, Any]:
        if app.state.sessions.get(sid) is None:
            raise HTTPException(
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
        rows = list(app.state.context_files.get(sid, {}).values())
        return {"files": rows}

    @app.post("/v1/sessions/{sid}/context/files")
    async def add_context_file(sid: str, request: Request) -> dict[str, Any]:
        """Attach a file to the session's context. Body: ``{path,
        mode?, size?, last_modified?, language?}``. Existing rows
        for the same path are upserted so the TUI can swap modes
        without racing an explicit delete.
        """

        sess = app.state.sessions.get(sid)
        if sess is None:
            raise HTTPException(
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
        body = {}
        try:
            body = await request.json()
        except Exception:
            body = {}
        if not isinstance(body, dict):
            body = {}
        path = (body.get("path") or "").strip()
        resolved_info = _resolve_context_attachment_path(
            sess=sess,
            raw_path=path,
            requested_workspace_id=str(body.get("workspace_id") or ""),
        )
        mode = body.get("mode") or "read"
        if mode not in {"edit", "read", "pin"}:
            raise HTTPException(
                status_code=422,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="bad_request",
                        message=(
                            f"invalid context file mode: {mode!r}; expected edit, read, or pin"
                        ),
                        details={"field": "mode", "allowed": ["edit", "read", "pin"]},
                        recoverable=True,
                    )
                ).model_dump(exclude_none=True),
            )
        resolved = Path(resolved_info["resolved_path"])
        if mode in {"read", "pin"}:
            if not resolved.exists():
                raise HTTPException(
                    status_code=404,
                    detail=ErrorEnvelope(
                        error=ErrorInfo(
                            error="context_file_error",
                            message=f"context file not found: {path}",
                            details={
                                "path": path,
                                "resolved_path": str(resolved),
                                "display_path": resolved_info.get("display_path") or path,
                                "workspace_id": resolved_info.get("workspace_id") or "",
                                "source": resolved_info.get("source") or "",
                                "mode": mode,
                                "operation": "exists",
                                "recovery_actions": [
                                    "choose_existing_file",
                                    "remove_context_file",
                                    "retry",
                                    "exit",
                                ],
                            },
                            recoverable=True,
                        )
                    ).model_dump(exclude_none=True),
                )
            if not resolved.is_file():
                raise HTTPException(
                    status_code=422,
                    detail=ErrorEnvelope(
                        error=ErrorInfo(
                            error="context_file_error",
                            message=f"context path is not a file: {path}",
                            details={
                                "path": path,
                                "resolved_path": str(resolved),
                                "display_path": resolved_info.get("display_path") or path,
                                "workspace_id": resolved_info.get("workspace_id") or "",
                                "source": resolved_info.get("source") or "",
                                "mode": mode,
                                "operation": "is_file",
                                "recovery_actions": [
                                    "choose_existing_file",
                                    "remove_context_file",
                                    "retry",
                                    "exit",
                                ],
                            },
                            recoverable=True,
                        )
                    ).model_dump(exclude_none=True),
                )
        row = {
            **resolved_info,
            "mode": mode,
            "added_at": datetime.now(timezone.utc).isoformat(),
            "last_modified": body.get("last_modified") or "",
            "size": int(body.get("size") or 0),
            "language": body.get("language") or "",
        }
        bucket = app.state.context_files.setdefault(sid, {})
        bucket[row["path"]] = row
        deps.flush_context_files(app)
        app.state.bus.publish(
            Event(
                type="context.file.added",
                session_id=sid,
                payload={"session_id": sid, "file": row},
            )
        )
        return row

    @app.delete("/v1/sessions/{sid}/context/files")
    async def remove_context_file(sid: str, request: Request) -> Response:
        """Detach a file by path. 204 whether the path was attached
        — the TUI fires this optimistically on `d` in the context
        pane and doesn't want to error if the file was already
        removed."""

        sess = app.state.sessions.get(sid)
        if sess is None:
            raise HTTPException(
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
        body = {}
        try:
            body = await request.json()
        except Exception:
            body = {}
        if not isinstance(body, dict):
            body = {}
        raw_path = (body.get("path") or "").strip()
        path = raw_path[1:].strip() if raw_path.startswith("@") else raw_path
        bucket = app.state.context_files.get(sid, {})
        matched_key = ""
        if path:
            for key, row in bucket.items():
                if path in {
                    key,
                    str(row.get("path") or ""),
                    str(row.get("display_path") or ""),
                    str(row.get("resolved_path") or ""),
                }:
                    matched_key = key
                    break
        if matched_key:
            deps.guard_direct_destructive_action(
                app,
                session_id=sid,
                workspace_id=sess.workspace_id,
                tool_name="gact.context_file.delete",
                args={"session_id": sid, "path": path},
                summary=f"detach context file {path} from session {sid}",
                reason="user_requested_context_file_delete",
            )
        removed = bucket.pop(matched_key, None) if matched_key else None
        if removed is not None:
            deps.flush_context_files(app)
            app.state.bus.publish(
                Event(
                    type="context.file.removed",
                    session_id=sid,
                    payload={"session_id": sid, "path": path},
                )
            )
        return Response(status_code=204)
