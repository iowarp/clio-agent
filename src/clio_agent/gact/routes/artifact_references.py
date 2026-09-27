"""Reference resolution route (A2 remote media): ``artifact:``/``resource:`` -> fetch path.

A viewer connected to a (possibly remote) CLIO cannot dereference an
``artifact:`` or ``resource:`` reference itself: the scheme means nothing to a
browser, and the bytes live behind the service's bearer. This route turns one
reference into its metadata plus the service-relative ``fetch_path`` of the
existing byte route, which the client then reads through its authenticated
transport. The grammar lives in :mod:`clio_agent.gact.artifacts.references`;
this module only binds it to HTTP and to the requesting session's workspace.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI


def register_artifact_reference_routes(app: FastAPI) -> None:
    """Register ``GET /v1/sessions/{sid}/references/resolve`` on ``app``."""

    from clio_agent.gact.artifacts.references import (  # noqa: PLC0415
        ReferenceResolutionError,
        resolve_reference,
    )
    from clio_agent.gact.routes.artifacts import (  # noqa: PLC0415
        _artifact_error,
        _audit,
        _session_workspace_id,
    )

    @app.get("/v1/sessions/{sid}/references/resolve")
    async def resolve_session_reference(sid: str, uri: str) -> dict[str, Any]:
        """Resolve one A2UI content reference for a viewer of session ``sid``.

        Returns ``{uri, kind, workspace_id, name, media_type, size_bytes,
        fetch_path, ...}``; ``fetch_path`` is a ``/v1/...`` route the client GETs
        with its bearer. Workspace-less ``resource`` forms resolve in the
        session's workspace. Failures are typed: ``reference_uri_invalid`` (422),
        ``reference_not_found`` (404), ``reference_workspace_unresolved`` (409),
        and an unknown session is ``not_found`` (404).
        """

        workspace_id = _session_workspace_id(app, sid)
        if workspace_id is None:
            raise _artifact_error(
                status_code=404,
                error="not_found",
                message=f"session not found: {sid}",
                details={"session_id": sid},
            )
        try:
            resolved = await resolve_reference(app, uri, session_workspace_id=workspace_id)
        except ReferenceResolutionError as exc:
            raise _artifact_error(
                status_code=exc.status_code,
                error=exc.error,
                message=exc.message,
                details=exc.details,
                recoverable=exc.status_code < 500,
            ) from exc
        _audit(app, route="resolve_session_reference", session_id=sid, kind=resolved["kind"])
        return resolved


__all__ = ["register_artifact_reference_routes"]
