"""Routes a surface uses for a client-rendered file view (FV-D3, FV-D4).

* ``PUT /v1/sessions/{sid}/file-views/{view_id}`` ``{holder, shown, file, state?}``: a
  mount of the view loaded (``shown``) or unloaded, with the file it shows
  (``{workspace_id, path, name, kind}``) and, optionally, its view state as readable lines.
  A mount re-sends it when the state changes. Showing re-sends every look still waiting on
  the view. Answers ``{shown}`` (whether any mount still holds it); a typed 422
  (error envelope: ``error.error`` is the reason) for a report over the limits.
* ``POST /v1/sessions/{sid}/file-views/{view_id}/looks/{call_id}``: the snapshot for one
  waiting look, the app look answer's shape (``{image, width, height, scale, painted,
  skipped}`` or ``{error: {reason, message}}``) plus ``state`` (the view state at that
  moment). 404 when no look of this view waits, 422 when the image is not a PNG.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from fastapi import FastAPI, HTTPException, Request

from clio_agent.gact.file_views import (
    FileViewInvalid,
    check_view_id,
    file_view_looks,
    file_view_table,
    publish_file_view_look,
    state_lines,
    view_file,
)
from clio_agent.gact.routes._body import json_body
from clio_agent.gact.types import ErrorEnvelope, ErrorInfo
from clio_agent.runtime import trace

__all__ = ["register_file_view_routes"]


def _refuse(exc: FileViewInvalid) -> HTTPException:
    trace.event("FILE_VIEW", "%s %s", exc.reason, exc.message)
    return HTTPException(
        status_code=422,
        detail=ErrorEnvelope(
            error=ErrorInfo(error=exc.reason, message=exc.message, recoverable=False)
        ).model_dump(exclude_none=True),
    )


def _session_or_404(app: FastAPI, sid: str) -> None:
    if app.state.sessions.get(sid) is None:
        raise HTTPException(status_code=404, detail="Session not found")


def register_file_view_routes(app: FastAPI) -> None:
    """Register the file view routes beside the app look routes."""

    @app.put("/v1/sessions/{sid}/file-views/{view_id}")
    async def put_file_view(sid: str, view_id: str, request: Request) -> dict[str, Any]:
        body = await json_body(request, route="PUT file view")
        shown = body.get("shown")
        if not isinstance(shown, bool):
            raise HTTPException(status_code=422, detail="shown must be a boolean")
        _session_or_404(app, sid)
        holder = str(body.get("holder") or "")[:120]
        looks = file_view_looks(app)
        try:
            check_view_id(view_id)
            if not shown:
                return {"shown": looks.set_shown(sid, view_id, holder, False)}
            file = view_file(body.get("file"))
            state = state_lines(body.get("state")) if "state" in body else None
        except FileViewInvalid as exc:
            raise _refuse(exc) from exc
        table = file_view_table(app)
        table.report(sid, view_id, file, state)
        held = looks.set_shown(sid, view_id, holder, True)
        table.prune(
            sid, {view.view_id for view in table.views(sid) if looks.shown(sid, view.view_id)}
        )
        for look in looks.pending_for_app(sid, view_id):
            publish_file_view_look(app, look)
        return {"shown": held}

    @app.post("/v1/sessions/{sid}/file-views/{view_id}/looks/{call_id}")
    async def post_file_view_look(
        sid: str, view_id: str, call_id: str, request: Request
    ) -> dict[str, Any]:
        from clio_agent.gact.mcp_app_look import (  # noqa: PLC0415
            LookAnswerInvalid,
            decode_snapshot_png,
        )

        _session_or_404(app, sid)
        body = await json_body(request, route="POST file view look")
        error = body.get("error")
        if isinstance(error, Mapping):
            answer: dict[str, Any] = {
                "error": {
                    "reason": str(error.get("reason") or "snapshot_failed")[:80],
                    "message": str(error.get("message") or "")[:2000],
                }
            }
        elif isinstance(body.get("image"), str):
            try:
                decode_snapshot_png(body["image"])
                state = state_lines(body.get("state"))
            except LookAnswerInvalid as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            except FileViewInvalid as exc:
                raise _refuse(exc) from exc
            answer = {
                "image": body["image"],
                "width": body.get("width"),
                "height": body.get("height"),
                "scale": body.get("scale"),
                "painted": body.get("painted") if isinstance(body.get("painted"), Mapping) else {},
                "skipped": [row for row in body.get("skipped") or [] if isinstance(row, Mapping)][
                    :64
                ],
                "state": state,
            }
        else:
            raise HTTPException(status_code=422, detail="image or error is required")
        if not file_view_looks(app).settle(sid, view_id, call_id, answer):
            raise HTTPException(status_code=404, detail="No look of this file view is waiting")
        return {}
