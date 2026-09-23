"""``delete_a2ui_surface`` — retire an existing A2UI surface (S4)."""

from __future__ import annotations

from typing import Any

from clio_agent.gact.a2ui_producer import _common
from clio_agent.gact.a2ui_producer._refusal import refusal
from clio_agent.gact.agents.tool_instrumentation import native_tool
from clio_agent.gact.protocol_v3 import A2UI_V091_WIRE


def build_delete_a2ui_surface_tool() -> Any:
    """Build the tool that deletes an existing live A2UI surface."""

    def delete_a2ui_surface(surface_id: str) -> dict[str, Any]:
        """Delete an existing A2UI surface; deletion is terminal.

        ``surface_id`` must name a live surface; an unknown or already
        deleted id is a typed refusal, not an error. A deleted id cannot be
        revised — reusing it with create_a2ui_surface mints a new surface.
        """

        resolved = _common.active_app_and_session()
        if isinstance(resolved, dict):
            return resolved
        app, session_id = resolved
        surface_id = surface_id.strip()
        existing = _common.existing_surface(app, session_id, surface_id)
        if existing is None or existing.state == "deleted":
            return refusal("a2ui_surface_not_found", detail=f"A2UI surface not found: {surface_id}")

        message = {"version": A2UI_V091_WIRE, "deleteSurface": {"surfaceId": surface_id}}
        outcome = _common.apply_messages(app, session_id, [message], catalog_id=existing.catalog_id)
        if isinstance(outcome, dict):
            return outcome
        surface = outcome.surfaces[-1]
        result: dict[str, Any] = {
            "rendered": True,
            "deleted": True,
            "catalog_id": surface.catalog_id,
            "session_id": session_id,
            "surface_id": surface.id,
            "revision": surface.revision,
            "state": surface.state,
        }
        result.update(_common.surface_registry_fields(outcome))
        return result

    return native_tool(
        delete_a2ui_surface,
        name="delete_a2ui_surface",
        presentation="text",
        title="Delete UI element",
        domain="surfaces",
        desc=delete_a2ui_surface.__doc__,
        args={
            "surface_id": {"type": "string", "description": "Existing live surface id to delete."}
        },
    )


__all__ = ["build_delete_a2ui_surface_tool"]
