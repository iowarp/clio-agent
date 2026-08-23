"""Expert-pack discovery + session-attachment routes for the GACT server (#714).

This concern owns the read/validate/attach surface for *expert packs* -- loose,
non-orchestrated collections of experts that a session can activate:

* ``GET /v1/expert-packs`` -- discover the installed packs for a workspace.
* ``GET /v1/expert-packs/{pack_id}`` -- resolve one pack to its agent hierarchy.
* ``POST /v1/expert-packs/validate`` -- validate a pack root on disk.
* ``GET /v1/sessions/{sid}/expert-pack`` -- read a session's active pack.
* ``POST /v1/sessions/{sid}/expert-pack`` -- set a session's active pack, by
  installed id or by an on-disk path.

The pack *install/update/delete* lifecycle is NOT here: a blueprint and a pack
share ONE install engine (``kind``-distinguished), so those thin aliases live
with :mod:`clio_agent.gact.routes.blueprints` -- one implementation, one
provenance model. This module covers only the discovery + session-attachment
surface.

The disk-reading primitives live in :mod:`clio_agent.gact.expert_packs` (single
source); the session-metadata reads reuse the byte-identical ``_runtime_*``
helpers in :mod:`clio_agent.gact.agents.resolution`. Handlers reach
``app.state`` directly and never import :mod:`clio_agent.gact.app`; the shared
:class:`~clio_agent.gact.routes.deps.GactDeps` is accepted for signature
uniformity with the other route factories.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from fastapi import FastAPI, HTTPException

from clio_agent.gact.agent_blueprints import (
    discover_agent_blueprints,
    load_agent_blueprints,
    validate_agent_hierarchy,
)
from clio_agent.gact.agents.resolution import (
    _runtime_active_session_expert_pack_id,
    _runtime_active_session_expert_pack_path,
    _runtime_workspace_catalog_cwd,
)
from clio_agent.gact.expert_packs import (
    discover_expert_packs,
    load_expert_packs,
    validate_expert_hierarchy,
    validate_expert_pack_path,
)
from clio_agent.gact.types import ErrorEnvelope, ErrorInfo, Session

if TYPE_CHECKING:
    from clio_agent.gact.routes.deps import GactDeps


def register_expert_packs_routes(app: FastAPI, deps: "GactDeps") -> None:
    """Register the expert-pack discovery + session-attachment routes on ``app``.

    Handlers close over the ``app`` argument (FastAPI's decorators need it) and
    reach ``app.state`` directly. ``deps``
    (:class:`~clio_agent.gact.routes.deps.GactDeps`) is accepted for signature
    uniformity with the other route factories.
    """

    @app.get("/v1/expert-packs")
    async def list_expert_packs(workspace_id: Optional[str] = None) -> dict[str, Any]:
        cwd = _runtime_workspace_catalog_cwd(app, workspace_id=workspace_id or "")
        packs = []
        for pack in discover_expert_packs(cwd=cwd):
            wire = pack.to_wire()
            wire["metadata"] = {**dict(wire.get("metadata") or {}), "lifecycle": "manual"}
            packs.append(wire)
        known_ids = {str(pack.get("id") or "") for pack in packs}
        for candidate in discover_agent_blueprints(cwd=cwd):
            wire = candidate.to_wire()
            if wire.get("kind") != "pack" or candidate.id in known_ids:
                continue
            wire["metadata"] = {**dict(wire.get("metadata") or {}), "lifecycle": "service"}
            packs.append(wire)
            known_ids.add(candidate.id)
        return {"expert_packs": packs}

    @app.get("/v1/expert-packs/{pack_id:path}")
    async def get_expert_pack(pack_id: str, workspace_id: Optional[str] = None) -> dict[str, Any]:
        cwd = _runtime_workspace_catalog_cwd(app, workspace_id=workspace_id or "")
        for pack in discover_expert_packs(cwd=cwd):
            if pack.id == pack_id:
                agents = validate_expert_hierarchy(load_expert_packs(cwd=cwd, pack_id=pack_id))
                wire = pack.to_wire()
                wire["metadata"] = {
                    **dict(wire.get("metadata") or {}),
                    "lifecycle": "manual",
                }
                return {
                    "expert_pack": wire,
                    "agents": [row.model_dump(exclude_none=True) for row in agents],
                }
        for pack in discover_agent_blueprints(cwd=cwd):
            wire = pack.to_wire()
            if pack.id != pack_id or wire.get("kind") != "pack":
                continue
            wire["metadata"] = {**dict(wire.get("metadata") or {}), "lifecycle": "service"}
            agents = validate_agent_hierarchy(
                load_agent_blueprints(cwd=cwd, blueprint_id=pack_id),
                blueprint=pack,
            )
            return {
                "expert_pack": wire,
                "agents": [row.model_dump(exclude_none=True) for row in agents],
            }
        raise HTTPException(
            status_code=404,
            detail=ErrorEnvelope(
                error=ErrorInfo(
                    error="not_found",
                    message=f"expert pack not found: {pack_id}",
                    details={"pack_id": pack_id, "workspace_id": workspace_id or ""},
                    recoverable=False,
                )
            ).model_dump(exclude_none=True),
        )

    @app.post("/v1/expert-packs/validate")
    async def validate_expert_pack(req: dict[str, Any]) -> dict[str, Any]:
        path = str(req.get("path") or "").strip()
        if not path:
            raise HTTPException(
                status_code=400,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="validation_error",
                        message="path is required",
                        recoverable=True,
                    )
                ).model_dump(exclude_none=True),
            )
        return validate_expert_pack_path(Path(path), scope=str(req.get("scope") or "session"))

    @app.get("/v1/sessions/{sid}/expert-pack")
    async def get_session_expert_pack(sid: str) -> dict[str, Any]:
        sess = app.state.sessions.get(sid)
        if sess is None:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="not_found",
                        message=f"session not found: {sid}",
                        details={"session_id": sid},
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )
        pack_id = _runtime_active_session_expert_pack_id(app, sid)
        pack_path = _runtime_active_session_expert_pack_path(app, sid)
        cwd = _runtime_workspace_catalog_cwd(app, session_id=sid)
        pack = next((row for row in discover_expert_packs(cwd=cwd) if row.id == pack_id), None)
        pack_wire: dict[str, Any] | None = pack.to_wire() if pack is not None else None
        if pack is None and pack_path is not None:
            validation = validate_expert_pack_path(pack_path, scope="session")
            raw_pack = validation.get("pack")
            pack_wire = raw_pack if isinstance(raw_pack, dict) else None
        return {
            "session_id": sid,
            "workspace_id": getattr(sess, "workspace_id", ""),
            "active_expert_pack_id": pack_id,
            "active_expert_pack_path": str(pack_path) if pack_path is not None else "",
            "expert_pack": pack_wire,
        }

    @app.post("/v1/sessions/{sid}/expert-pack")
    async def set_session_expert_pack(sid: str, req: dict[str, Any]) -> dict[str, Any]:
        sess = app.state.sessions.get(sid)
        if sess is None:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="not_found",
                        message=f"session not found: {sid}",
                        details={"session_id": sid},
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )
        pack_id = str(req.get("pack_id") or "").strip()
        pack_path = str(req.get("path") or req.get("pack_path") or "").strip()
        cwd = _runtime_workspace_catalog_cwd(app, session_id=sid)
        if pack_path:
            validation = validate_expert_pack_path(Path(pack_path), scope="session")
            if not validation.get("enabled", False):
                raise HTTPException(
                    status_code=400,
                    detail=ErrorEnvelope(
                        error=ErrorInfo(
                            error="validation_error",
                            message="expert pack path is invalid",
                            details={
                                "path": pack_path,
                                "validation_errors": validation.get("validation_errors", []),
                            },
                            recoverable=True,
                        )
                    ).model_dump(exclude_none=True),
                )
            pack_wire = validation["pack"]
            updated = app.state.sessions.update(
                sid,
                metadata_patch={
                    "active_expert_pack_id": str(pack_wire.get("id") or ""),
                    "active_expert_pack_version": str(pack_wire.get("version") or ""),
                    "active_expert_pack_scope": "session",
                    "active_expert_pack_definition_path": str(
                        pack_wire.get("definition_path") or ""
                    ),
                    "active_expert_pack_path": str(Path(pack_path).expanduser()),
                },
            )
            return {
                "session_id": sid,
                "workspace_id": getattr(sess, "workspace_id", ""),
                "active_expert_pack_id": str(pack_wire.get("id") or ""),
                "active_expert_pack_path": str(Path(pack_path).expanduser()),
                "expert_pack": pack_wire,
                "session": Session(**updated.to_wire()).model_dump(exclude_none=True)
                if updated
                else None,
            }
        if not pack_id:
            raise HTTPException(
                status_code=400,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="validation_error",
                        message="pack_id or path is required",
                        recoverable=True,
                    )
                ).model_dump(exclude_none=True),
            )
        pack = next((row for row in discover_expert_packs(cwd=cwd) if row.id == pack_id), None)
        if pack is None:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="not_found",
                        message=f"expert pack not found: {pack_id}",
                        details={"pack_id": pack_id, "session_id": sid},
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )
        updated = app.state.sessions.update(
            sid,
            metadata_patch={
                "active_expert_pack_id": pack.id,
                "active_expert_pack_version": pack.version,
                "active_expert_pack_scope": pack.scope,
                "active_expert_pack_definition_path": str(pack.manifest_path or pack.root),
                "active_expert_pack_path": "",
            },
        )
        return {
            "session_id": sid,
            "workspace_id": getattr(sess, "workspace_id", ""),
            "active_expert_pack_id": pack.id,
            "expert_pack": pack.to_wire(),
            "session": Session(**updated.to_wire()).model_dump(exclude_none=True)
            if updated
            else None,
        }
