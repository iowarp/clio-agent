"""``update_a2ui_data_model`` — set or delete a value in a surface's data model (S4)."""

from __future__ import annotations

from typing import Any

from clio_agent.gact.a2ui_producer import _common
from clio_agent.gact.a2ui_producer._presentation import surface_presentation
from clio_agent.gact.a2ui_producer._refusal import refusal
from clio_agent.gact.agents.tool_instrumentation import native_tool
from clio_agent.gact.protocol_v3 import A2UI_V091_WIRE


def build_update_a2ui_data_model_tool() -> Any:
    """Build the tool that sets or deletes one path in a surface's data model."""

    def update_a2ui_data_model(
        surface_id: str,
        path: str = "/",
        value: Any = None,
        delete: bool = False,
    ) -> dict[str, Any]:
        """Set or delete a value in an existing A2UI surface's data model.

        ``path`` is an absolute JSON Pointer ("/" means the whole model).
        ``delete=True`` removes the key at ``path`` (the protocol's
        omitted-value delete form; ``value`` is ignored); otherwise the value
        at ``path`` is replaced (``value=None`` sets it to null).
        """

        resolved = _common.active_app_and_session()
        if isinstance(resolved, dict):
            return resolved
        app, session_id = resolved
        surface_id = surface_id.strip()
        existing = _common.existing_surface(app, session_id, surface_id)
        if existing is None or existing.state == "deleted":
            return refusal("a2ui_surface_not_found", detail=f"A2UI surface not found: {surface_id}")

        payload: dict[str, Any] = {"surfaceId": surface_id, "path": path}
        if not delete:
            payload["value"] = value
        message = {"version": A2UI_V091_WIRE, "updateDataModel": payload}
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
        update_a2ui_data_model,
        name="update_a2ui_data_model",
        presentation=surface_presentation,
        desc=update_a2ui_data_model.__doc__,
        title="Update UI element",
        domain="surfaces",
        args={
            "surface_id": {"type": "string", "description": "Existing live surface id."},
            "path": {
                "type": "string",
                "description": "Absolute JSON Pointer path; '/' means the whole model.",
            },
            "value": {"description": "Replacement value at path (ignored when delete=true)."},
            "delete": {
                "type": "boolean",
                "description": "Remove the key at path instead of setting it.",
            },
        },
    )


__all__ = ["build_update_a2ui_data_model_tool"]
