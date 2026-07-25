"""Artifact RO-Crate export HTTP routes (#966 S7 / #973, item 3).

The "give me the scripts" surface: package a session's (or a single artifact's
lineage's) registered artifacts + TransformRecords into an RO-Crate zip bundle —
``ro-crate-metadata.json`` (JSON-LD PROV), the artifact bytes under ``data/``, and
a compiled ``reproduce.py`` + ``reproduce.ipynb`` (per-stage sha256 asserts). Two
routes, registered from
:func:`clio_agent.gact.routes.artifacts.register_artifacts_routes` (no-accretion,
its own owner module like ``artifact_lineage``):

* ``GET /v1/artifacts/{artifact_id}/export`` — one artifact's lineage bundle;
* ``GET /v1/sessions/{sid}/export/bundle`` — the whole session's bundle
  (``?include_children`` unions the delegates' workspaces).

A shipped bundle registers the content hashes it exported as CAS GC roots
(:func:`register_export_gc_roots`) so a user is never handed bytes the reachability
GC later evicts (closing S6's loop, #972). Bundle assembly does blocking file I/O
(reading CAS/workspace bytes) and the registry's first access folds the event log,
so both routes offload to a worker thread — a first access on the event loop would
raise :class:`RegistryFoldOnLoopError`.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import Response

from clio_agent.gact.artifacts.export import (
    ExportBundle,
    build_artifact_bundle,
    build_session_bundle,
    register_export_gc_roots,
)
from clio_agent.gact.runtime.retention import enforce_list_bound
from clio_agent.gact.types import ErrorEnvelope, ErrorInfo


def _export_error(
    *, status_code: int, error: str, message: str, details: Optional[dict[str, Any]] = None
) -> HTTPException:
    """Build an ``ErrorEnvelope``-wrapped :class:`HTTPException` (SPEC §6.0)."""
    return HTTPException(
        status_code=status_code,
        detail=ErrorEnvelope(
            error=ErrorInfo(error=error, message=message, details=details or {}, recoverable=False)
        ).model_dump(exclude_none=True),
    )


def _audit(app: FastAPI, *, route: str, **fields: Any) -> None:
    """Append one bounded row to the artifact-route audit ledger."""
    if not hasattr(app.state, "artifact_route_audit"):
        app.state.artifact_route_audit = []
    app.state.artifact_route_audit.append({"route": route, "at": time.time(), **fields})
    enforce_list_bound(app, app.state.artifact_route_audit, "artifact_route_audit")


def _zip_response(bundle: ExportBundle, *, filename: str) -> Response:
    """Serialize + register-roots + wrap a bundle as a downloadable zip response."""
    payload = bundle.to_zip()
    return Response(
        content=payload,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def register_artifact_export_routes(app: FastAPI) -> None:
    """Register the RO-Crate export routes on ``app`` (#973)."""

    @app.get("/v1/artifacts/{artifact_id}/export")
    async def export_artifact(artifact_id: str) -> Response:
        """Export one artifact's lineage as an RO-Crate zip (bytes + reproduce.py).

        The crate carries the artifact's version chain, the producing
        TransformRecords (as CreateActions), the one-hop input records, and a
        compiled ``reproduce.py``. An unknown ``artifact_id`` is an honest ``404``.
        """
        bundle = await asyncio.to_thread(build_artifact_bundle, app, artifact_id)
        if bundle is None:
            raise _export_error(
                status_code=404,
                error="not_found",
                message=f"artifact not found: {artifact_id}",
                details={"artifact_id": artifact_id},
            )
        register_export_gc_roots(app, bundle.workspace_id, bundle.crate_shas)
        _audit(
            app,
            route="export_artifact",
            artifact_id=artifact_id,
            files=len(bundle.files),
            pinned_shas=len(bundle.crate_shas),
        )
        return _zip_response(bundle, filename=f"{artifact_id}.crate.zip")

    @app.get("/v1/sessions/{sid}/export/bundle")
    async def export_session_bundle(sid: str, include_children: bool = True) -> Response:
        """Export a session's artifacts + transforms as an RO-Crate zip.

        ``?include_children=true`` (default) unions the descendant child sessions'
        workspaces so a parent orchestrator's export carries its delegates' outputs.
        An unknown session is an honest ``404``.
        """
        bundle = await asyncio.to_thread(
            build_session_bundle, app, sid, include_children=include_children
        )
        if bundle is None:
            raise _export_error(
                status_code=404,
                error="not_found",
                message=f"session not found: {sid}",
                details={"session_id": sid},
            )
        register_export_gc_roots(app, bundle.workspace_id, bundle.crate_shas)
        _audit(
            app,
            route="export_session_bundle",
            session_id=sid,
            files=len(bundle.files),
            pinned_shas=len(bundle.crate_shas),
            include_children=include_children,
        )
        return _zip_response(bundle, filename=f"session-{sid}.crate.zip")


__all__ = ["register_artifact_export_routes"]
