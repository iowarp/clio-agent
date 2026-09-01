"""Registration seam for artifact sub-concerns."""

from __future__ import annotations

from fastapi import FastAPI


def register_artifact_extension_routes(app: FastAPI) -> None:
    """Register alias, lineage, export, and tabular-preview routes."""

    from clio_agent.gact.routes.artifact_aliases import register_artifact_alias_routes
    from clio_agent.gact.routes.artifact_export import register_artifact_export_routes
    from clio_agent.gact.routes.artifact_lineage import register_artifact_lineage_routes
    from clio_agent.gact.routes.artifact_table_preview import register_artifact_table_preview_routes

    register_artifact_alias_routes(app)
    register_artifact_lineage_routes(app)
    register_artifact_export_routes(app)
    register_artifact_table_preview_routes(app)
