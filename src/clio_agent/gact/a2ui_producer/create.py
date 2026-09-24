"""``create_a2ui_surface`` — create or revise a trusted A2UI surface (S4)."""

from __future__ import annotations

from typing import Any, Optional

from clio_agent.gact.a2ui_capability_selection import select_catalog
from clio_agent.gact.a2ui_producer import _common
from clio_agent.gact.a2ui_producer._presentation import surface_presentation
from clio_agent.gact.a2ui_producer._refusal import catalog_selection_refusal, refusal
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
        new one (the result's ``created`` reports which happened). A new
        surface uses ``catalog_id``, or this agent's default catalog if empty.

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
        if not is_new:
            # Locked per surface: an existing surface's own catalog wins
            # regardless of what this call's catalog_id argument says.
            resolved_catalog_id = existing.catalog_id
        else:
            # A NEW surface always crosses the client-preference gate, even
            # with an explicit catalog_id: "preferred wins only when it is
            # itself in both the client-supported and the producible set"
            # (docs/gact/a2ui-binding.md) applies to every caller, not only
            # the empty-catalog_id default -- a caller that names a specific
            # catalog either gets exactly that one or a typed refusal, never
            # a silently substituted different one and never a bypass of
            # negotiation altogether.
            preferred = catalog_id.strip() or None
            selection = select_catalog(app, session_id, preferred=preferred)
            if not selection.ok:
                return catalog_selection_refusal(selection, preferred=preferred)
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
        domain="surfaces",
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
                    "Catalog id for a NEW surface (must be both client-"
                    "advertised and one this agent declares, or the call is "
                    "refused); empty selects this agent's first declared catalog "
                    "the client supports."
                ),
            },
        },
    )


__all__ = ["build_create_a2ui_surface_tool"]
