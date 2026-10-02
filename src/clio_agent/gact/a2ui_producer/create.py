"""``create_a2ui_surface`` — create or revise a trusted A2UI surface (S4)."""

from __future__ import annotations

from typing import Any, Optional

from clio_agent.gact.a2ui_capability_selection import select_catalog
from clio_agent.gact.a2ui_producer import (
    _chart_spec,
    _common,
    _data_reference,
    _definition_artifact,
    _export,
)
from clio_agent.gact.a2ui_producer._presentation import surface_presentation
from clio_agent.gact.a2ui_producer._refusal import catalog_selection_refusal, refusal
from clio_agent.gact.agents.tool_instrumentation import native_tool
from clio_agent.gact.protocol_v3 import A2UI_V091_WIRE


def build_create_a2ui_surface_tool() -> Any:
    """Build the root-agent tool that creates or revises a trusted surface."""

    def create_a2ui_surface(
        surface_id: str,
        components: Optional[list[dict[str, Any]]] = None,
        components_path: str = "",
        data_model: Optional[dict[str, Any]] = None,
        catalog_id: str = "",
    ) -> dict[str, Any]:
        """Create or update an inline interactive view or widget in this conversation.

        Reuse an existing ``surface_id`` to revise in place; call
        ``inspect_a2ui_surface`` when the id is missing from this turn.
        Pass exactly one of ``components`` or ``components_path``.
        Load skill ``a2ui-catalog-<slug>`` for guidance and inspect
        ``catalog.json#/components/<ExactComponentId>`` for its schema.
        Charts, maps, tables, drafts, weather, steps, and other components
        render inline. The renderer supplies selection, zoom, and export.
        """

        resolved = _common.active_app_and_session()
        if isinstance(resolved, dict):
            return resolved
        app, session_id = resolved
        surface_id = surface_id.strip()

        components_resolved = _common.resolve_components(
            app, session_id, components, components_path
        )
        if isinstance(components_resolved, dict):
            return components_resolved
        components = components_resolved
        root_components = [c for c in components if c.get("id") == "root"]
        if len(root_components) != 1:
            return refusal(
                "a2ui_validation_failed",
                detail='A2UI surface components must contain exactly one id="root" component',
            )

        exported = _export.export_workspace_paths(app, session_id, components)
        if isinstance(exported, dict):
            return exported
        components, export_report = exported

        data_reference_error = _data_reference.validate_component_data_references(app, components)
        if data_reference_error is not None:
            return data_reference_error

        chart_spec_error = _chart_spec.validate_chart_components(components)
        if chart_spec_error is not None:
            return chart_spec_error

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
            # A refused batch is never minted/overwritten (#1533 adversarial
            # review): nothing was applied, so the surface's definition
            # artifact -- if it already had one -- is untouched.
            return outcome
        surface = outcome.surfaces[-1]

        # Mint AFTER apply succeeds, from the FULL merged component list (this
        # call may only have touched a subset of a multi-component surface):
        # a definition artifact only ever records a state the surface really
        # reached.
        merge_base = None if is_new else existing
        merged_components = _common.merged_surface_components(merge_base, components)
        definition = _definition_artifact.mint_surface_definition_artifact(
            app, session_id, surface_id, merged_components
        )
        if "definition_artifact_id" not in definition:
            return definition

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
        result.update(export_report)
        result.update(_common.surface_registry_fields(outcome))
        result.update(definition)
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
                "description": (
                    "Component definitions in root-first order. Exactly one of "
                    "components or components_path is required. A dataUri/url/uri "
                    "value may be a plain path inside this session's workspace; "
                    "it is exported to artifact:// automatically before validation."
                ),
            },
            "components_path": {
                "type": "string",
                "description": (
                    "Workspace JSON file holding the components array, instead "
                    "of passing components inline."
                ),
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
