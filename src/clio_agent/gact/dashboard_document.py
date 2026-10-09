"""Validate an authored dashboard with the existing A2UI catalog and data model."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal, Mapping

from fastapi import FastAPI
from pydantic import BaseModel, ConfigDict, Field

from clio_agent.gact.a2ui import apply_batch
from clio_agent.gact.a2ui_capability_selection import select_catalog
from clio_agent.gact.a2ui_catalogs.activation import (
    session_catalog_resolver,
    session_producible_catalog_ids,
)
from clio_agent.gact.a2ui_producer import _chart_spec, _common, _data_reference, _export
from clio_agent.gact.artifacts.minting import _contained, _session_workspace_id, _workspace_root
from clio_agent.gact.protocol_v3 import A2UI_V091_WIRE


class DashboardDocument(BaseModel):
    """Editable source for a whole dashboard, accumulated over multiple agent steps."""

    model_config = ConfigDict(extra="forbid")
    format: Literal["clio.dashboard.document.v1"]
    title: str = Field(min_length=1, max_length=200)
    components: list[dict[str, Any]] = Field(min_length=1)
    data_model: dict[str, Any] = Field(default_factory=dict)
    catalog_id: str = ""
    source_surface_ids: list[str] = Field(default_factory=list, max_length=200)


def read_dashboard_document(app: FastAPI, sid: str, definition_path: str) -> DashboardDocument:
    """Read an existing dashboard source inside the session's workspace."""
    root = _workspace_root(app, _session_workspace_id(app, sid))
    if root is None:
        raise ValueError("This session has no workspace.")
    path = Path(definition_path)
    if not path.is_absolute():
        path = root / path
    path = path.resolve()
    if not _contained(path, root):
        raise ValueError("The dashboard document must be inside the workspace.")
    if path.stat().st_size > 10_000_000:
        raise ValueError("The definition exceeds 10 MB; reference data as artifacts.")
    return DashboardDocument.model_validate_json(path.read_bytes())


def compile_dashboard_surface(
    app: FastAPI, sid: str, document: DashboardDocument, report_id: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Validate the authored system using normal producer gates without creating a chat view."""
    tree_error = _common.component_tree_error(document.components)
    if tree_error or sum(component.get("id") == "root" for component in document.components) != 1:
        raise ValueError(tree_error or "The dashboard needs exactly one root component.")
    exported = _export.export_workspace_paths(app, sid, document.components)
    if isinstance(exported, dict):
        raise ValueError(json.dumps(exported))
    components, export_report = exported
    # Preserve the agent's chosen groups while isolating this artifact's cameras.
    for component in components:
        if isinstance(component.get("syncGroup"), str) and component["syncGroup"]:
            component["syncGroup"] = f"dashboard:{report_id}:{component['syncGroup']}"
    reference_error = _data_reference.validate_component_data_references(app, components)
    if reference_error:
        raise ValueError(json.dumps(reference_error))
    chart_error = _chart_spec.validate_chart_components(components)
    if chart_error:
        raise ValueError(json.dumps(chart_error))
    selection = select_catalog(app, sid, preferred=document.catalog_id or None)
    if not selection.ok:
        raise ValueError(f"The dashboard catalog could not be selected: {selection}")
    surface_id = f"dashboard_{report_id}"
    messages: list[Mapping[str, Any]] = [
        {
            "version": A2UI_V091_WIRE,
            "createSurface": {
                "surfaceId": surface_id,
                "catalogId": selection.catalog_id,
            },
        },
        {
            "version": A2UI_V091_WIRE,
            "updateComponents": {
                "surfaceId": surface_id,
                "components": components,
            },
        },
        {
            "version": A2UI_V091_WIRE,
            "updateDataModel": {
                "surfaceId": surface_id,
                "value": document.data_model,
            },
        },
    ]
    folded, _ = apply_batch(
        {},
        sid,
        messages,
        catalogs=session_catalog_resolver(app, sid),
        producible=frozenset(session_producible_catalog_ids(app, sid)),
        part_id=f"dashboard_{report_id}",
    )
    return folded[(sid, surface_id)].to_wire(), export_report
