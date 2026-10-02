"""Registration seam for artifact sub-concerns."""

from __future__ import annotations

from fastapi import FastAPI


def register_artifact_extension_routes(app: FastAPI) -> None:
    """Register alias, lineage, export, tabular-preview/query, and reference routes."""

    from clio_agent.gact.routes.artifact_aliases import register_artifact_alias_routes
    from clio_agent.gact.routes.artifact_export import register_artifact_export_routes
    from clio_agent.gact.routes.artifact_lineage import register_artifact_lineage_routes
    from clio_agent.gact.routes.artifact_raster_query import register_artifact_raster_query_routes
    from clio_agent.gact.routes.artifact_references import register_artifact_reference_routes
    from clio_agent.gact.routes.artifact_table_export import register_artifact_table_export_routes
    from clio_agent.gact.routes.artifact_table_preview import register_artifact_table_preview_routes
    from clio_agent.gact.routes.artifact_table_query import register_artifact_table_query_routes

    register_artifact_alias_routes(app)
    register_artifact_lineage_routes(app)
    register_artifact_export_routes(app)
    register_artifact_table_preview_routes(app)
    register_artifact_table_query_routes(app)
    register_artifact_table_export_routes(app)
    register_artifact_raster_query_routes(app)
    register_artifact_reference_routes(app)
