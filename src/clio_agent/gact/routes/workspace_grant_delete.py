"""Workspace additional-root grant revocation route."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException, Request

from clio_agent.gact.types import ErrorEnvelope, ErrorInfo


def register_workspace_grant_delete_route(app: FastAPI) -> None:
    """Register removal of a user-granted additional workspace folder."""

    @app.delete("/v1/workspaces/{wid}/grants")
    async def delete_workspace_grant(wid: str, request: Request) -> dict[str, Any]:
        if app.state.workspaces.get(wid) is None:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="not_found",
                        message=f"workspace not found: {wid}",
                        details={"workspace_id": wid},
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )
        kind = str(request.query_params.get("kind") or "fs_root")
        pattern = str(request.query_params.get("pattern") or "").strip()
        if kind not in {"root", "fs_root"} or not pattern:
            raise HTTPException(
                status_code=400,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="invalid_request",
                        message="grant removal requires kind=fs_root and a non-empty pattern",
                        recoverable=True,
                    )
                ).model_dump(exclude_none=True),
            )
        from clio_agent.gact.runtime import grants  # noqa: PLC0415

        return {"workspace_id": wid, "grant": grants.revoke_root_grant(app, wid, pattern)}
