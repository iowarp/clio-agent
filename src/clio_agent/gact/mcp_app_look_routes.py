"""Routes a surface uses so the agent can look at the view it holds (CM1).

Bound to the app by the same capability as the other app routes (``data_ref``, checked
by the ``resolve`` the app routes pass in).

* ``PUT .../shown`` ``{holder, shown}``: the surface mounted the view (or unloaded it).
  Showing it re-sends every look still waiting on the app, so a surface that mounts
  after a look was published answers it.
* ``POST .../looks/{call_id}`` ``{image, width, height, scale, painted, skipped}`` or
  ``{error: {reason, message}}``: the snapshot for one waiting look (404 when none waits,
  422 when the image is not a PNG).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI, HTTPException, Request

from clio_agent.gact.mcp_app_look import (
    LookAnswerInvalid,
    decode_snapshot_png,
    look_table,
    publish_look,
)
from clio_agent.gact.routes._body import json_body

if TYPE_CHECKING:
    from clio_agent.gact.mcp_apps import MCPAppRecord

__all__ = ["register_app_look_routes"]


def register_app_look_routes(
    app: FastAPI, resolve: Callable[[str, str, str], "MCPAppRecord"]
) -> None:
    """Register the look routes beside the other app routes."""

    @app.put("/v1/sessions/{sid}/mcp-apps/{app_id}/shown")
    async def put_mcp_app_shown(
        sid: str, app_id: str, data_ref: str, request: Request
    ) -> dict[str, Any]:
        resolve(sid, app_id, data_ref)
        body = await json_body(request, route="PUT MCP App shown")
        shown = body.get("shown")
        if not isinstance(shown, bool):
            raise HTTPException(status_code=422, detail="shown must be a boolean")
        table = look_table(app)
        held = table.set_shown(sid, app_id, str(body.get("holder") or ""), shown)
        if held:
            for look in table.pending_for_app(sid, app_id):
                publish_look(app, look)
        return {"shown": held}

    @app.post("/v1/sessions/{sid}/mcp-apps/{app_id}/looks/{call_id}")
    async def post_mcp_app_look(
        sid: str, app_id: str, call_id: str, data_ref: str, request: Request
    ) -> dict[str, Any]:
        resolve(sid, app_id, data_ref)
        body = await json_body(request, route="POST MCP App look")
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
            except LookAnswerInvalid as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            answer = {
                "image": body["image"],
                "width": body.get("width"),
                "height": body.get("height"),
                "scale": body.get("scale"),
                "painted": body.get("painted") if isinstance(body.get("painted"), Mapping) else {},
                "skipped": [row for row in body.get("skipped") or [] if isinstance(row, Mapping)][
                    :64
                ],
            }
        else:
            raise HTTPException(status_code=422, detail="image or error is required")
        if not look_table(app).settle(sid, app_id, call_id, answer):
            raise HTTPException(status_code=404, detail="No look of this app is waiting")
        return {}
