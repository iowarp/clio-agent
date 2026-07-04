"""Declarative event-hook CRUD routes for the GACT server (#714).

SPEC §6.17 declarative hooks: ``id`` + ``event`` + (``command`` | ``url``) +
optional ``session_id`` / ``workspace_id`` scope. The gact-tui ``gact hook``
subcommand reads/writes them through this surface:

* ``GET /v1/hooks`` -- list every registered declarative hook.
* ``POST /v1/hooks`` -- create one (event + command|url required).
* ``DELETE /v1/hooks/{hook_id}`` -- remove one, gated by the shared
  direct-destructive-action permission guard.

These are DISTINCT from :mod:`clio_agent.runtime.hooks` -- the in-process
Python hooks the framework fires on tool/message events. The declarative hooks
here are gact-tui-driven, in-memory (no persistence), and live on
``app.state.declarative_hooks``.

The delete route reaches the shared direct-destructive-action permission guard
through :class:`~clio_agent.gact.routes.deps.GactDeps`; the module imports only
leaf packages (types, stdlib) and never loads :mod:`clio_agent.gact.app`.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response

from clio_agent.gact.routes._body import json_body
from clio_agent.gact.types import ErrorEnvelope, ErrorInfo

if TYPE_CHECKING:
    from clio_agent.gact.routes.deps import GactDeps


def register_hooks_routes(app: FastAPI, deps: "GactDeps") -> None:
    """Register the SPEC §6.17 declarative event-hook CRUD routes on ``app``.

    Handlers close over the ``app`` argument (FastAPI's decorators need it) and
    read/write the live registry via ``app.state.declarative_hooks``; the delete
    route reaches the cross-concern permission guard through ``deps``.
    """

    @app.get("/v1/hooks")
    async def list_hooks() -> dict[str, Any]:
        return {"hooks": list(app.state.declarative_hooks.values())}

    @app.post("/v1/hooks")
    async def create_hook(request: Request) -> dict[str, Any]:
        body = await json_body(request, route="POST /v1/hooks")
        event = (body.get("event") or "").strip()
        if not event:
            raise HTTPException(
                status_code=400,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="invalid_request",
                        message="hook missing required field: event",
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )
        if not (body.get("command") or body.get("url")):
            raise HTTPException(
                status_code=400,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="invalid_request",
                        message="hook needs command or url",
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )
        hid = body.get("id") or f"hook_{uuid.uuid4().hex[:12]}"
        row = {
            "id": hid,
            "event": event,
            "command": body.get("command") or "",
            "url": body.get("url") or "",
            "session_id": body.get("session_id") or "",
            "workspace_id": body.get("workspace_id") or "",
        }
        app.state.declarative_hooks[hid] = row
        return row

    @app.delete("/v1/hooks/{hook_id}")
    async def delete_hook(hook_id: str) -> Response:
        hook = app.state.declarative_hooks.get(hook_id)
        if hook is None:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="not_found",
                        message=f"hook not found: {hook_id}",
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )
        deps.guard_direct_destructive_action(
            app,
            session_id=str(hook.get("session_id") or ""),
            workspace_id=str(hook.get("workspace_id") or ""),
            tool_name="gact.hook.delete",
            args={"hook_id": hook_id},
            summary=f"delete hook {hook_id}",
            reason="user_requested_hook_delete",
        )
        app.state.declarative_hooks.pop(hook_id, None)
        return Response(status_code=204)
