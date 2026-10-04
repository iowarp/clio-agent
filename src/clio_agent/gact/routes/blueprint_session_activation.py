"""Activate an Agent Blueprint on a session (the ``POST /v1/sessions/{sid}/agent-blueprint``
body).

Validation, discovery and activation record resolution reasons in clio-core (store
writes), so the route runs this whole function off the event loop.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException

from clio_agent.gact import session_warmup
from clio_agent.gact.agent_blueprints import (
    discover_agent_blueprints,
    runtime_tool_names_for_validation,
    validate_agent_blueprint_path,
)
from clio_agent.gact.agents.resolution import _runtime_workspace_catalog_cwd
from clio_agent.gact.blueprint_identity import identity_fields, select_blueprint
from clio_agent.gact.types import ErrorEnvelope, ErrorInfo, Session


def activate_session_blueprint(
    app: FastAPI,
    deps: Any,
    sid: str,
    req: dict[str, Any],
    *,
    not_found: Callable[..., HTTPException],
) -> dict[str, Any]:
    """Validate, resolve and activate the requested blueprint on session ``sid``."""
    sess = app.state.sessions.get(sid)
    if sess is None:
        raise not_found(f"session not found: {sid}", session_id=sid)
    blueprint_id = str(req.get("blueprint_id") or req.get("agent_blueprint_id") or "").strip()
    blueprint_path = str(req.get("path") or req.get("blueprint_path") or "").strip()
    cwd = _runtime_workspace_catalog_cwd(app, session_id=sid)
    if blueprint_path:
        validation = validate_agent_blueprint_path(
            Path(blueprint_path),
            scope="session",
            runtime_tool_names=runtime_tool_names_for_validation(app),
        )
        blueprint_wire = validation["agent_blueprint"]
        if not validation.get("enabled", False):
            from clio_agent.gact.agent_blueprint_requires import (  # noqa: PLC0415
                path_activation_invalid_http_exception,
            )

            raise path_activation_invalid_http_exception(
                validation, blueprint_path, blueprint_wire, app=app, session_id=sid
            )
        install_root = Path(str(blueprint_wire.get("root") or blueprint_path)).expanduser()
        activation_metadata = deps.agent_blueprint_activation_metadata(
            blueprint_wire=blueprint_wire,
            install_root=install_root,
            scope="session",
            session_id=sid,
        )
        updated = app.state.sessions.update(
            sid,
            metadata_patch={
                **activation_metadata,
                "active_agent_blueprint_path": str(Path(blueprint_path).expanduser()),
                "active_expert_pack_id": "",
                "active_expert_pack_path": "",
            },
        )
    else:
        if not blueprint_id:
            raise HTTPException(
                status_code=400,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="validation_error",
                        message="blueprint_id or path is required",
                        recoverable=True,
                    )
                ).model_dump(exclude_none=True),
            )
        blueprint = select_blueprint(discover_agent_blueprints(cwd=cwd), blueprint_id)
        if blueprint is None:
            raise not_found(
                f"agent blueprint not found: {blueprint_id}",
                agent_blueprint_id=blueprint_id,
                session_id=sid,
            )
        blueprint_wire = blueprint.to_wire()
        activation_metadata = deps.agent_blueprint_activation_metadata(
            blueprint_wire=blueprint_wire,
            install_root=blueprint.root,
            scope=blueprint.scope,
            session_id=sid,
        )
        updated = app.state.sessions.update(
            sid,
            metadata_patch={
                **activation_metadata,
                "active_agent_blueprint_path": str(blueprint.root),
                "active_agent_blueprint_identity": identity_fields(blueprint)["identity"],
                "active_expert_pack_id": "",
                "active_expert_pack_path": "",
            },
        )
    # The blueprint's servers start now, not on the session's next message.
    session_warmup.start_session_warmup(app, sid, trigger="blueprint_activated")
    return {
        "session_id": sid,
        "workspace_id": getattr(sess, "workspace_id", ""),
        "active_agent_blueprint_id": str(blueprint_wire.get("id") or ""),
        "active_agent_blueprint_path": str(blueprint_path),
        "agent_blueprint": blueprint_wire,
        "session": Session(**updated.to_wire()).model_dump(exclude_none=True) if updated else None,
    }
