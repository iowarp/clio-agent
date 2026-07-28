"""Registration facade for artifacts and their document experiences."""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import FastAPI

from clio_agent.gact.routes.artifacts import register_artifacts_routes
from clio_agent.gact.routes.documents import register_document_routes

if TYPE_CHECKING:
    from clio_agent.gact.routes.deps import GactDeps


def register_artifact_workspace_routes(app: FastAPI, deps: "GactDeps") -> None:
    """Register artifact registry routes and document-specific extensions."""

    register_artifacts_routes(app, deps)
    register_document_routes(app, deps)


__all__ = ["register_artifact_workspace_routes"]
