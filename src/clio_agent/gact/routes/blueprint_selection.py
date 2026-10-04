"""Shared HTTP validation for blueprint selection and new-session defaults."""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from typing import Any

from fastapi import HTTPException

from clio_agent.gact.agent_blueprint_requires import (
    AgentBlueprintInstallRefused,
    install_refusal_http_exception,
)
from clio_agent.gact.agents.resolution import _runtime_workspace_catalog_cwd
from clio_agent.gact.blueprint_activation import agent_blueprint_activation_metadata
from clio_agent.gact.blueprint_catalog import materialize_blueprint
from clio_agent.gact.blueprint_identity import AmbiguousBlueprintError, identity_fields
from clio_agent.gact.blueprint_reload import apply_blueprint_change


def selection_errors(change: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    """Translate missing or invalid sources without masking an identity conflict."""
    try:
        return change()
    except AmbiguousBlueprintError:
        raise
    except AgentBlueprintInstallRefused as exc:
        raise install_refusal_http_exception(exc) from exc
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        raise HTTPException(
            status_code=404 if isinstance(exc, FileNotFoundError) else 400,
            detail={
                "error": {
                    "error": "blueprint_selection_failed",
                    "message": str(exc),
                    "recoverable": True,
                }
            },
        ) from exc


async def prepare_session_blueprint(app: Any, workspace_id: str, metadata: dict[str, Any]) -> None:
    """Materialize an explicitly selected/default blueprint before session creation.

    A failure leaves no empty session behind. Runtime copies are owned by the
    connected CLIO, and their exact identity and checksum follow the session.
    """
    identifier = str(
        metadata.get("active_agent_blueprint_identity")
        or metadata.get("active_agent_blueprint_id")
        or ""
    )
    if not identifier:
        return

    def prepare() -> dict[str, Any]:
        blueprint = materialize_blueprint(
            identifier,
            cwd=_runtime_workspace_catalog_cwd(app, workspace_id=workspace_id),
            workspace_id=workspace_id,
            app=app,
        )
        patch = agent_blueprint_activation_metadata(
            blueprint_wire=blueprint.to_wire(),
            install_root=blueprint.root,
            scope=blueprint.scope,
            app=app,
        )
        return {
            "metadata": {
                **patch,
                "active_agent_blueprint_identity": identity_fields(blueprint)["identity"],
                "active_agent_blueprint_path": str(blueprint.root),
            }
        }

    result = await apply_blueprint_change(
        app, lambda: selection_errors(prepare), label="Prepare session blueprint"
    )
    metadata.update(result["metadata"])
