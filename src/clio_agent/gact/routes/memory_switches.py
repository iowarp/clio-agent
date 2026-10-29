"""Per-workspace and per-session memory switches.

* **Use account memory** (workspace, default on): off means sessions in the workspace
  read and write only that workspace's project memory.
* **Memory off** (session): chosen before the first message, recorded on the session,
  and never turned back on for it.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict

from clio_agent.gact.memory.policy import SESSION_MEMORY_OFF, WORKSPACE_USE_ACCOUNT
from clio_agent.gact.types import ErrorEnvelope, ErrorInfo


class WorkspaceMemory(BaseModel):
    """The workspace's memory switch."""

    model_config = ConfigDict(extra="forbid")
    use_account_memory: bool


class SessionMemory(BaseModel):
    """The session's memory switch (only ``off: true`` before the first message)."""

    model_config = ConfigDict(extra="forbid")
    off: bool


def _error(status: int, code: str, message: str, **details: Any) -> HTTPException:
    return HTTPException(
        status_code=status,
        detail=ErrorEnvelope(
            error=ErrorInfo(error=code, message=message, details=details, recoverable=True)
        ).model_dump(exclude_none=True),
    )


def register_memory_switch_routes(app: FastAPI) -> None:
    """``/v1/workspaces/{wid}/memory`` and ``/v1/sessions/{sid}/memory``."""

    @app.get("/v1/workspaces/{wid}/memory")
    def get_workspace_memory(wid: str) -> dict[str, Any]:
        workspace = app.state.workspaces.get(wid)
        if workspace is None:
            raise _error(404, "not_found", f"workspace not found: {wid}", workspace_id=wid)
        return {"use_account_memory": bool(workspace.config.get(WORKSPACE_USE_ACCOUNT, True))}

    @app.put("/v1/workspaces/{wid}/memory")
    def put_workspace_memory(wid: str, body: WorkspaceMemory) -> dict[str, Any]:
        workspace = app.state.workspaces.get(wid)
        if workspace is None:
            raise _error(404, "not_found", f"workspace not found: {wid}", workspace_id=wid)
        workspace.config[WORKSPACE_USE_ACCOUNT] = body.use_account_memory
        app.state.workspaces.update(wid, metadata_patch=None)
        return {"use_account_memory": body.use_account_memory}

    @app.put("/v1/sessions/{sid}/memory")
    def put_session_memory(sid: str, body: SessionMemory) -> dict[str, Any]:
        session = app.state.sessions.get(sid)
        if session is None:
            raise _error(404, "not_found", f"session not found: {sid}", session_id=sid)
        current = bool((session.metadata or {}).get(SESSION_MEMORY_OFF))
        if body.off == current:
            return {"memory_off": current}
        if not body.off:
            raise _error(
                409, "memory_off_is_final", "Memory stays off for this conversation", session_id=sid
            )
        if session.message_count or app.state.messages.get(sid):
            raise _error(
                409,
                "memory_off_after_start",
                "Memory can only be turned off before the first message",
                session_id=sid,
            )
        app.state.sessions.update(sid, metadata_patch={SESSION_MEMORY_OFF: True})
        return {"memory_off": True}


__all__ = ["register_memory_switch_routes"]
