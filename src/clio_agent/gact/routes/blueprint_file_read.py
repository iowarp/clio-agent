"""Blueprint file listing and raw content reads, scoped to the connected CLIO."""

from __future__ import annotations

from typing import Any, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import Response

from clio_agent.gact.agent_blueprint_files import (
    BlueprintPathEscapesRootError,
    is_textual_blueprint_file,
    list_blueprint_files,
    resolve_agent_blueprint_root,
    resolve_blueprint_file_path,
)
from clio_agent.gact.types import ErrorEnvelope, ErrorInfo


def register_blueprint_file_read_routes(app: FastAPI) -> None:
    """Register installed file reads without exposing unrelated host paths."""

    @app.get("/v1/agent-blueprints/{blueprint_id}/files")
    async def list_agent_blueprint_files(
        blueprint_id: str,
        workspace_id: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> dict[str, Any]:
        """iowarp/clio-agent#1192 -- flat recursive listing of a blueprint root.

        Mirrors ``GET /v1/workspaces/{wid}/files``'s conventions (path/type/
        size relative to the root, capped walk, skip cost-walking dirs) but
        scoped to an agent-blueprint root. ``session_id`` additionally
        resolves a PATH-activated blueprint (see
        :func:`clio_agent.gact.agent_blueprint_files.resolve_agent_blueprint_root`)
        when its own id matches ``blueprint_id`` -- the demo case
        (``earthscope-flat``) where a blueprint is activated by on-disk path
        rather than by installed id. Registered ahead of the greedy
        ``{blueprint_id:path}`` GET below so it is not shadowed.
        """

        root = resolve_agent_blueprint_root(
            app, blueprint_id, workspace_id=workspace_id or "", session_id=session_id or ""
        )
        if root is None:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="not_found",
                        message=f"agent blueprint not found: {blueprint_id}",
                        details={"agent_blueprint_id": blueprint_id},
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )
        return {"entries": list_blueprint_files(root)}

    @app.get("/v1/agent-blueprints/{blueprint_id}/files/read")
    async def read_agent_blueprint_file(
        blueprint_id: str,
        path: str,
        workspace_id: Optional[str] = None,
        session_id: Optional[str] = None,
        draft: bool = False,
    ) -> Response:
        """iowarp/clio-agent#1192 -- raw content read for one blueprint file.

        Path-traversal-hardened to the blueprint root (a ``..`` escape is a
        typed 400); a missing file is a typed 404. Text is served decoded as
        ``text/plain``, binary raw with its real content type, mirroring
        ``GET /v1/workspaces/{wid}/files/read`` (#673, #676).
        """

        root = resolve_agent_blueprint_root(
            app, blueprint_id, workspace_id=workspace_id or "", session_id=session_id or ""
        )
        if root is None:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="not_found",
                        message=f"agent blueprint not found: {blueprint_id}",
                        details={"agent_blueprint_id": blueprint_id},
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )
        try:
            if draft:
                from clio_agent.gact.blueprint_drafts import draft_view

                root = draft_view(root)
            target = resolve_blueprint_file_path(root, path)
        except BlueprintPathEscapesRootError:
            raise HTTPException(
                status_code=400,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="path_outside_blueprint",
                        message=f"path escapes blueprint root: {path}",
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            ) from None
        if not target.is_file():
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="not_found",
                        message=f"file not found: {path}",
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )
        try:
            data = target.read_bytes()
        except OSError as exc:
            raise HTTPException(
                status_code=500,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="read_failed",
                        message=f"could not read file: {exc}",
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            ) from exc
        if is_textual_blueprint_file(target.name, data):
            return Response(
                content=data.decode("utf-8", errors="replace"),
                media_type="text/plain; charset=utf-8",
            )
        import mimetypes  # noqa: PLC0415

        guessed, _ = mimetypes.guess_type(target.name)
        return Response(content=data, media_type=guessed or "application/octet-stream")
