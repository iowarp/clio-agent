"""``update_a2ui_components`` — upsert components on an existing surface (S4)."""

from __future__ import annotations

from typing import Any

from clio_agent.gact.a2ui_producer import _common
from clio_agent.gact.a2ui_producer._presentation import surface_presentation
from clio_agent.gact.a2ui_producer._refusal import refusal
from clio_agent.gact.agents.tool_instrumentation import native_tool
from clio_agent.gact.protocol_v3 import A2UI_V091_WIRE


def build_update_a2ui_components_tool() -> Any:
    """Build the tool that upserts components on an existing live surface."""

    def update_a2ui_components(surface_id: str, components: list[dict[str, Any]]) -> dict[str, Any]:
        """Replace one or more components on an already-created A2UI surface.

        ``surface_id`` must name a live (non-deleted) surface from a prior
        create_a2ui_surface result's ``session_surface_ids``; an unknown or
        deleted id is a typed refusal, not an error.

        Component shapes and guidance: load_skill("a2ui-catalog-<slug>");
        one component: load_skill(..., file="catalog.json#/components/<Name>").
        """

        resolved = _common.active_app_and_session()
        if isinstance(resolved, dict):
            return resolved
        app, session_id = resolved
        surface_id = surface_id.strip()
        existing = _common.existing_surface(app, session_id, surface_id)
        if existing is None or existing.state == "deleted":
            return refusal("a2ui_surface_not_found", detail=f"A2UI surface not found: {surface_id}")

        message = {
            "version": A2UI_V091_WIRE,
            "updateComponents": {"surfaceId": surface_id, "components": components},
        }
        outcome = _common.apply_messages(app, session_id, [message], catalog_id=existing.catalog_id)
        if isinstance(outcome, dict):
            return outcome
        surface = outcome.surfaces[-1]
        result: dict[str, Any] = {
            "rendered": True,
            "created": False,
            "catalog_id": surface.catalog_id,
            "session_id": session_id,
            "surface_id": surface.id,
            "part_id": surface.part_id,
            "revision": surface.revision,
            "state": surface.state,
        }
        result.update(_common.surface_registry_fields(outcome))
        return result

    return native_tool(
        update_a2ui_components,
        name="update_a2ui_components",
        presentation=surface_presentation,
        desc=update_a2ui_components.__doc__,
        title="Update UI element",
        args={
            "surface_id": {"type": "string", "description": "Existing live surface id."},
            "components": {
                "type": "array",
                "items": {"type": "object"},
                "description": "Component definitions to upsert on this surface.",
            },
        },
    )


__all__ = ["build_update_a2ui_components_tool"]
