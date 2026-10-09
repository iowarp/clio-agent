"""Read current surfaces for corrections and follow-ups about selected items."""

from __future__ import annotations

import json
from typing import Any

from clio_agent.gact.a2ui_producer import _common
from clio_agent.gact.a2ui_producer._refusal import refusal
from clio_agent.gact.agents.tool_instrumentation import native_tool

MAX_DEFINITION_CHARS = 100_000


def build_inspect_a2ui_surface_tool() -> Any:
    """Build the read-only, session-scoped surface inspection tool."""

    def inspect_a2ui_surface(surface_id: str = "", artifact_id: str = "") -> dict[str, Any]:
        """Find an existing inline view or widget for a correction or follow-up.

        With no id, list this conversation's live surfaces.
        With an id, read components, data-model updates and declared bindings.
        artifact_id inspects a pinned saved dashboard version instead.
        Fresh viewers report camera, active tabs, bound values and readiness.
        Use expected revisions when updating; capture_a2ui_surface returns
        the real rendered pixels for visual investigation and review.
        """

        resolved = _common.active_app_and_session()
        if isinstance(resolved, dict):
            return resolved
        app, session_id = resolved
        requested_id = surface_id.strip()
        if artifact_id:
            from types import SimpleNamespace

            from clio_agent.gact.dashboard_reports import read_dashboard_report

            try:
                saved = read_dashboard_report(app, session_id, artifact_id)["surface"]
                surface = SimpleNamespace(**saved)
                requested_id = surface.id
            except ValueError as exc:
                return refusal("a2ui_capture_artifact_unavailable", detail=str(exc))
        if requested_id:
            if not artifact_id:
                surface = app.state.a2ui_store.get(session_id, requested_id)
            if surface is None or surface.state == "deleted":
                return refusal(
                    "a2ui_surface_not_found", detail=f"A2UI surface not found: {requested_id}"
                )
            components = _common.current_surface_components(surface)
            data_model_messages = [
                message for message in surface.messages if "updateDataModel" in message
            ]
            definition = {"components": components, "data_model_messages": data_model_messages}
            if len(json.dumps(definition, ensure_ascii=False)) > MAX_DEFINITION_CHARS:
                return {
                    "surface_id": surface.id,
                    "revision": surface.revision,
                    "component_count": len(components),
                    "definition_too_large": True,
                    "detail": "This definition is too large to return through a tool result.",
                }
            available_viewers = [
                row
                for row in app.state.a2ui_visual.viewers(session_id, requested_id)
                if row["artifact_id"] == artifact_id
            ]
            viewers: list[dict[str, Any]] = []
            viewer_chars = 0
            for row in available_viewers:
                size = len(json.dumps(row, ensure_ascii=False))
                if len(viewers) >= 4 or viewer_chars + size > 70_000:
                    continue
                viewers.append(row)
                viewer_chars += size
            return {
                "surface_id": surface.id,
                "revision": surface.revision,
                "catalog_id": surface.catalog_id,
                "artifact_id": artifact_id,
                "viewers": viewers,
                "viewers_omitted": len(available_viewers) - len(viewers),
                "controls": declared_controls(app, surface.catalog_id, components),
                **definition,
            }

        rows = [
            row
            for row in app.state.a2ui_store.list_wire(session_id)
            if row.get("state") != "deleted"
        ]
        summaries: list[dict[str, Any]] = []
        for row in rows[-32:]:
            components = _common.current_surface_components(
                app.state.a2ui_store.get(session_id, str(row["id"]))
            )
            named = next((item for item in components if item.get("title")), None)
            content = next((item for item in components if item.get("component") != "Column"), None)
            summaries.append(
                {
                    "surface_id": row["id"],
                    "revision": row["revision"],
                    "title": named.get("title") if named else "",
                    "component": content.get("component") if content else "",
                }
            )
        return {"surfaces": summaries, "total": len(rows), "truncated": len(rows) > 32}

    return native_tool(
        inspect_a2ui_surface,
        name="inspect_a2ui_surface",
        title="Inspect widget",
        domain="surfaces",
        presentation="text",
        read_only=True,
        desc=inspect_a2ui_surface.__doc__,
        args={
            "artifact_id": {
                "type": "string",
                "description": "Pinned saved dashboard version, or empty for live surfaces.",
            },
            "surface_id": {
                "type": "string",
                "description": "Existing id, or empty to list this session's surfaces.",
            },
        },
    )


__all__ = ["build_inspect_a2ui_surface_tool"]


def declared_controls(
    app: Any, catalog_id: str, components: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Expose real schema locations and bindings without inventing control paths."""
    entry = app.state.a2ui_catalogs.get(catalog_id)
    if entry is None:
        return []
    controls: list[dict[str, Any]] = []

    def walk(value: Any, prop: str, component: dict[str, Any]) -> None:
        if isinstance(value, dict) and set(value) == {"path"}:
            controls.append(
                {
                    "component_id": component["id"],
                    "property": prop,
                    "path": value["path"],
                    "owner": "data_model",
                    "schema": f"catalog.json#/components/{component['component']}",
                }
            )
        elif isinstance(value, dict):
            for key, item in value.items():
                walk(item, f"{prop}/{key}", component)
        elif isinstance(value, list):
            for index, item in enumerate(value):
                walk(item, f"{prop}/{index}", component)

    for component in components:
        if component.get("component") not in entry.file["components"]:
            continue
        for key, item in component.items():
            walk(item, key, component)
    return controls
