"""``create_a2ui_surface`` — create or revise a trusted A2UI surface (S4)."""

from __future__ import annotations

from typing import Any, Optional

from clio_agent.gact.a2ui_capability_selection import select_catalog
from clio_agent.gact.a2ui_producer import _common
from clio_agent.gact.a2ui_producer._presentation import surface_presentation
from clio_agent.gact.a2ui_producer._refusal import refusal
from clio_agent.gact.agents.tool_instrumentation import native_tool
from clio_agent.gact.protocol_v3 import A2UI_V091_WIRE


def build_create_a2ui_surface_tool() -> Any:
    """Build the root-agent tool that creates or revises a trusted surface."""

    def create_a2ui_surface(
        surface_id: str,
        components: list[dict[str, Any]],
        data_model: Optional[dict[str, Any]] = None,
        catalog_id: str = "",
    ) -> dict[str, Any]:
        """Create or update an interactive analysis surface in this conversation.

        ``surface_id`` selects the surface: reuse an id from a prior result's
        ``session_surface_ids`` to revise it in place; any other id creates a
        new one (the result's ``created`` reports which happened).
        ``catalog_id`` left empty auto-selects the client's preferred
        producible catalog for a new surface.

        Component shapes and guidance: load_skill("a2ui-catalog-<slug>");
        one component: load_skill(..., file="catalog.json#/components/<Name>").
        """

        resolved = _common.active_app_and_session()
        if isinstance(resolved, dict):
            return resolved
        app, session_id = resolved
        surface_id = surface_id.strip()
        root_components = [c for c in components if c.get("id") == "root"]
        if len(root_components) != 1:
            return refusal(
                "a2ui_validation_failed",
                detail='A2UI surface components must contain exactly one id="root" component',
            )

        existing = _common.existing_surface(app, session_id, surface_id)
        is_new = existing is None or existing.state == "deleted"
        resolved_catalog_id = catalog_id.strip()
        if not resolved_catalog_id:
            if not is_new:
                resolved_catalog_id = existing.catalog_id
            else:
                selection = select_catalog(app, session_id)
                if not selection.ok:
                    assert selection.reason is not None
                    return refusal(
                        selection.reason,
                        detail=(
                            "no catalog_id was given and catalog selection did not "
                            "resolve one for this session"
                        ),
                    )
                resolved_catalog_id = selection.catalog_id or ""

        messages: list[dict[str, Any]] = []
        if is_new:
            messages.append(
                {
                    "version": A2UI_V091_WIRE,
                    "createSurface": {
                        "surfaceId": surface_id,
                        "catalogId": resolved_catalog_id,
                    },
                }
            )
        messages.append(
            {
                "version": A2UI_V091_WIRE,
                "updateComponents": {"surfaceId": surface_id, "components": components},
            }
        )
        if data_model is not None:
            messages.append(
                {
                    "version": A2UI_V091_WIRE,
                    "updateDataModel": {
                        "surfaceId": surface_id,
                        "path": "/",
                        "value": data_model,
                    },
                }
            )

        outcome = _common.apply_messages(app, session_id, messages, catalog_id=resolved_catalog_id)
        if isinstance(outcome, dict):
            return outcome
        surface = outcome.surfaces[-1]
        result: dict[str, Any] = {
            "rendered": True,
            "created": surface.id in outcome.created_surface_ids,
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
        create_a2ui_surface,
        name="create_a2ui_surface",
        presentation=surface_presentation,
        desc=create_a2ui_surface.__doc__,
        title="Generate UI element",
        args={
            "surface_id": {
                "type": "string",
                "description": (
                    "Stable surface id. Reuse an id from a previous result's "
                    "session_surface_ids to revise that surface; any other id "
                    "creates a new one."
                ),
            },
            "components": {
                "type": "array",
                "items": {"type": "object"},
                "description": "Component definitions in root-first order.",
            },
            "data_model": {
                "type": "object",
                "description": "Optional root data model for component path bindings.",
            },
            "catalog_id": {
                "type": "string",
                "description": (
                    "Producible catalog id, or empty to auto-select the client's "
                    "preferred producible catalog for a new surface."
                ),
            },
        },
    )


__all__ = ["build_create_a2ui_surface_tool"]
