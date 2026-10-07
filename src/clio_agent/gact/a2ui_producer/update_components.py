"""``update_a2ui_components`` — upsert components on an existing surface (S4)."""

from __future__ import annotations

from typing import Any, Optional

from clio_agent.gact.a2ui_producer import (
    _chart_spec,
    _common,
    _data_reference,
    _definition_artifact,
    _export,
)
from clio_agent.gact.a2ui_producer._presentation import surface_presentation
from clio_agent.gact.a2ui_producer._refusal import refusal
from clio_agent.gact.agents.tool_instrumentation import native_tool
from clio_agent.gact.protocol_v3 import A2UI_V091_WIRE


def build_update_a2ui_components_tool() -> Any:
    """Build the tool that upserts components on an existing live surface."""

    def update_a2ui_components(
        surface_id: str,
        components: Optional[list[dict[str, Any]]] = None,
        components_path: str = "",
    ) -> dict[str, Any]:
        """Replace one or more components on an existing inline view or widget.

        Find the live ``surface_id`` with ``inspect_a2ui_surface`` if needed.
        Pass exactly one of ``components`` or ``components_path``.
        Load ``a2ui-catalog-<slug>`` for guidance; inspect one schema at
        ``catalog.json#/components/<ExactComponentId>``. The renderer owns
        pan, zoom, selection, export, and Reference this controls.
        For trajectory maps, revise the data reference or track/order fields;
        the renderer draws the paths from the resulting rows. Revise
        ``filterFields`` when the useful exploration dimensions change.
        """

        resolved = _common.active_app_and_session()
        if isinstance(resolved, dict):
            return resolved
        app, session_id = resolved
        surface_id = surface_id.strip()
        existing = _common.existing_surface(app, session_id, surface_id)
        if existing is None or existing.state == "deleted":
            return refusal("a2ui_surface_not_found", detail=f"A2UI surface not found: {surface_id}")

        components_resolved = _common.resolve_components(
            app, session_id, components, components_path
        )
        if isinstance(components_resolved, dict):
            return components_resolved
        components = components_resolved

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

        message = {
            "version": A2UI_V091_WIRE,
            "updateComponents": {"surfaceId": surface_id, "components": components},
        }
        outcome = _common.apply_messages(app, session_id, [message], catalog_id=existing.catalog_id)
        if isinstance(outcome, dict):
            # A refused batch is never minted/overwritten (#1533 adversarial
            # review): nothing was applied, so the surface's definition
            # artifact keeps whatever it already recorded.
            return outcome
        surface = outcome.surfaces[-1]

        # Mint AFTER apply succeeds, from the FULL merged component list --
        # this call may only upsert a SUBSET of a multi-component surface, so
        # the stored definition must be the whole live surface, never just
        # this call's own payload.
        merged_components = _common.merged_surface_components(existing, components)
        definition = _definition_artifact.mint_surface_definition_artifact(
            app, session_id, surface_id, merged_components
        )
        if "definition_artifact_id" not in definition:
            return definition

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
        result.update(export_report)
        result.update(_common.surface_registry_fields(outcome))
        result.update(definition)
        return result

    return native_tool(
        update_a2ui_components,
        name="update_a2ui_components",
        presentation=surface_presentation,
        desc=update_a2ui_components.__doc__,
        title="Update widget",
        domain="surfaces",
        args={
            "surface_id": {"type": "string", "description": "Existing live surface id."},
            "components": {
                "type": "array",
                "items": {"type": "object"},
                "description": (
                    "Component definitions to upsert on this surface. Exactly "
                    "one of components or components_path is required. A "
                    "dataUri/url/uri value may be a plain path inside this "
                    "session's workspace; it is exported to artifact:// "
                    "automatically before validation."
                ),
            },
            "components_path": {
                "type": "string",
                "description": (
                    "Workspace JSON file holding the components array, instead "
                    "of passing components inline."
                ),
            },
        },
    )


__all__ = ["build_update_a2ui_components_tool"]
