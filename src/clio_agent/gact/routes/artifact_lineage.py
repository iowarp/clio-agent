"""Artifact lineage + transform HTTP routes (#966 S5 / #971).

The outbound query surface for TransformRecords + the lineage graph (owner
decision #966.6). Three routes, registered from
:func:`clio_agent.gact.routes.artifacts.register_artifacts_routes` (no-accretion —
its own owner module, like ``artifact_aliases``):

* ``GET /v1/artifacts/{artifact_id}/lineage?direction=&depth=`` — the provenance
  graph rooted at a version (nodes ``artifact|activity|gap``; edges
  ``used|generated|revision_of`` + evidence), either direction, bounded depth;
* ``GET /v1/sessions/{sid}/transforms`` — the TransformRecords a session produced;
* ``GET /v1/transforms/{activity_id}`` — one TransformRecord by its ``call_id``.

Follows the ``artifacts.py`` / ``memory.py`` pattern: ``ErrorEnvelope`` typed
errors, a clamped ``depth``, a bounded audit ledger, and the registry's first
access offloaded to a worker thread (the boot fold's synchronous I/O must never
run on the event loop).
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Optional

from fastapi import FastAPI, HTTPException

from clio_agent.gact.artifacts.registry import ArtifactRegistry, get_registry
from clio_agent.gact.runtime.retention import enforce_list_bound
from clio_agent.gact.types import ErrorEnvelope, ErrorInfo

_DEPTH_DEFAULT = 3
_DEPTH_MAX = 12


def _lineage_error(
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
    """Append one bounded row to the lineage-route audit ledger."""
    if not hasattr(app.state, "artifact_route_audit"):
        app.state.artifact_route_audit = []
    app.state.artifact_route_audit.append({"route": route, "at": time.time(), **fields})
    enforce_list_bound(app, app.state.artifact_route_audit, "artifact_route_audit")


async def _registry(app: FastAPI) -> ArtifactRegistry:
    """Return the app's artifact registry, offloading a first-access boot fold."""
    existing = getattr(app.state, "artifact_registry", None)
    if existing is not None:
        return existing
    return await asyncio.to_thread(get_registry, app)


def _clamp_depth(depth: Optional[int]) -> int:
    """Clamp ``depth`` into ``[0, _DEPTH_MAX]`` (default when unset)."""
    if depth is None:
        return _DEPTH_DEFAULT
    if depth < 0:
        return 0
    return min(depth, _DEPTH_MAX)


def _session_workspace_id(app: FastAPI, sid: str) -> Optional[str]:
    """The workspace id bound to a session, or ``None`` when the session is unknown."""
    store = getattr(app.state, "sessions", None) or getattr(app.state, "session_store", None)
    if store is None:
        return None
    session = store.get(sid)
    if session is None:
        return None
    return str(getattr(session, "workspace_id", "") or "")


def register_artifact_lineage_routes(app: FastAPI) -> None:
    """Register the lineage + transform read routes on ``app`` (#971)."""

    @app.get("/v1/artifacts/{artifact_id}/lineage")
    async def artifact_lineage(
        artifact_id: str, direction: str = "both", depth: int | None = None
    ) -> dict[str, Any]:
        """The provenance graph rooted at a version (both directions, bounded depth).

        ``direction`` ∈ ``{upstream, downstream, both}`` (an unknown value defaults
        to ``both``); ``depth`` is clamped to ``[0, 12]``. An unknown ``artifact_id``
        is an honest ``404``.
        """
        requested_depth = _clamp_depth(depth)
        backend = getattr(app.state, "artifact_provenance_backend", None)
        lineage = getattr(backend, "lineage", None)
        try:
            if callable(lineage):
                graph = await asyncio.to_thread(
                    lineage,
                    artifact_id,
                    direction=direction,
                    depth=requested_depth,
                )
                provider_name = str(getattr(backend, "provider_name", "native"))
            else:
                from clio_agent.gact.artifacts.lineage import build_lineage  # noqa: PLC0415

                registry = await _registry(app)
                graph = build_lineage(
                    registry,
                    artifact_id,
                    direction=direction,
                    depth=requested_depth,
                )
                provider_name = "native"
        except Exception as exc:
            raise _lineage_error(
                status_code=503,
                error="artifact_provenance_query_failed",
                message="selected artifact provenance provider could not answer lineage",
                details={"artifact_id": artifact_id, "reason": f"{type(exc).__name__}: {exc}"},
            ) from exc
        if graph is None:
            raise _lineage_error(
                status_code=404,
                error="not_found",
                message=f"artifact not found: {artifact_id}",
                details={"artifact_id": artifact_id},
            )
        _audit(
            app,
            route="artifact_lineage",
            artifact_id=artifact_id,
            direction=graph["direction"],
            nodes=len(graph["nodes"]),
            provider=provider_name,
        )
        return graph

    @app.get("/v1/sessions/{sid}/transforms")
    async def session_transforms(sid: str, include_children: bool = False) -> dict[str, Any]:
        """The TransformRecords a session produced (activity records, trace-only).

        ``?include_children=true`` (GAP B, S5 #971) AGGREGATES the descendant child
        sessions' records too: a parent ORCHESTRATOR session delegates the tool work
        to spawned children, so its OWN records are empty while the children hold
        everything. With the flag set, the child sessions are resolved via the
        agent-task registry (``child_session_id`` on the parent's tasks, bounded
        descendants) and their transforms merged. Each row already carries its
        producing ``session_id``, so attribution is per-row; the body also lists the
        aggregated ``child_session_ids``. Flag off → the session's own records only,
        byte-identical to before.
        """
        if _session_workspace_id(app, sid) is None:
            raise _lineage_error(
                status_code=404,
                error="not_found",
                message=f"session not found: {sid}",
                details={"session_id": sid},
            )
        registry = await _registry(app)
        transforms = list(registry.transforms_for_session(sid))
        if not include_children:
            _audit(app, route="session_transforms", session_id=sid, returned=len(transforms))
            return {
                "transforms": [t.to_payload() for t in transforms],
                "count": len(transforms),
            }
        from clio_agent.gact.agent_tasks import descendant_session_ids  # noqa: PLC0415

        child_ids = descendant_session_ids(app, sid)
        for child in child_ids:
            transforms.extend(registry.transforms_for_session(child))
        _audit(
            app,
            route="session_transforms",
            session_id=sid,
            returned=len(transforms),
            include_children=True,
            children=len(child_ids),
        )
        return {
            "transforms": [t.to_payload() for t in transforms],
            "count": len(transforms),
            "include_children": True,
            "child_session_ids": child_ids,
        }

    @app.get("/v1/transforms/{activity_id}")
    async def get_transform(activity_id: str) -> dict[str, Any]:
        """One TransformRecord by its ``call_id`` (the activity id)."""
        registry = await _registry(app)
        transform = registry.get_transform(activity_id)
        if transform is None:
            raise _lineage_error(
                status_code=404,
                error="not_found",
                message=f"transform not found: {activity_id}",
                details={"activity_id": activity_id},
            )
        _audit(app, route="get_transform", activity_id=activity_id)
        return {"transform": transform.to_payload()}


__all__ = ["register_artifact_lineage_routes"]
