"""``update_a2ui_data_model`` — set or delete a value in a surface's data model (S4)."""

from __future__ import annotations

from typing import Any

from clio_agent.gact.a2ui_producer import _bound_files, _common
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
        expected_revision: int | None = None,
        artifact_id: str = "",
        viewer_id: str = "",
        expected_view_revision: int | None = None,
    ) -> dict[str, Any]:
        """Set or delete a value in an existing A2UI surface's data model.

        Live definition (empty artifact_id): absolute JSON Pointer; "/" replaces
        the model. delete=True removes the key; value=None sets it to null.
        Use declared filter, selection or view bindings: unbound paths do not
        control a viewer. Acceptance does not establish rendered image review.
        Saved dashboard (artifact_id): change ONE EXACT declared control path
        from inspect_a2ui_surface.controls, not a parent of several controls.
        Pass its inspected viewer id and exact view epoch; inspect again before
        changing another control.
        """

        resolved = _common.active_app_and_session()
        if isinstance(resolved, dict):
            return resolved
        app, session_id = resolved
        surface_id = surface_id.strip()
        if artifact_id:
            return _control_saved_view(
                app,
                session_id,
                surface_id,
                artifact_id,
                expected_revision,
                viewer_id,
                expected_view_revision,
                path,
                value,
                delete,
            )
        existing = _common.existing_surface(app, session_id, surface_id)
        if existing is None or existing.state == "deleted":
            return refusal("a2ui_surface_not_found", detail=f"A2UI surface not found: {surface_id}")

        exported: list[dict[str, Any]] = []
        if not delete:
            components = _common.merged_surface_components(existing, [])
            modelled = _bound_files.export_bound_values(app, session_id, components, path, value)
            if isinstance(modelled, dict):
                return modelled
            value, exported = modelled
        payload: dict[str, Any] = {"surfaceId": surface_id, "path": path}
        if not delete:
            payload["value"] = value
        message = {"version": A2UI_V091_WIRE, "updateDataModel": payload}
        outcome = _common.apply_messages(
            app,
            session_id,
            [message],
            catalog_id=existing.catalog_id,
            expected_revisions={surface_id: expected_revision}
            if expected_revision is not None
            else None,
        )
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
        if exported:
            result["exported_artifacts"] = exported
        result.update(_common.surface_registry_fields(outcome))
        return result

    return native_tool(
        update_a2ui_data_model,
        name="update_a2ui_data_model",
        presentation=surface_presentation,
        desc=update_a2ui_data_model.__doc__,
        title="Update widget",
        domain="surfaces",
        args={
            "surface_id": {"type": "string", "description": "Existing live surface id."},
            "expected_revision": {
                "type": "integer",
                "description": "Inspected revision; a concurrent change rejects this update.",
            },
            "artifact_id": {
                "type": "string",
                "description": "Pinned saved dashboard version for a local view change, or empty for a live definition.",
            },
            "viewer_id": {
                "type": "string",
                "description": "Inspected viewer id; required for saved view controls.",
            },
            "expected_view_revision": {
                "type": "integer",
                "description": "Exact inspected viewer epoch; required for saved view controls.",
            },
            "path": {
                "type": "string",
                "description": "Live definition: absolute JSON Pointer ('/' is the whole model). Saved dashboard: one exact declared control path from inspect_a2ui_surface.controls, never a parent container.",
            },
            "value": {"description": "Replacement value at path (ignored when delete=true)."},
            "delete": {
                "type": "boolean",
                "description": "Remove the key at path instead of setting it.",
            },
        },
    )


__all__ = ["build_update_a2ui_data_model_tool"]


def _control_saved_view(
    app: Any,
    sid: str,
    surface_id: str,
    artifact_id: str,
    revision: int | None,
    viewer_id: str,
    view_revision: int | None,
    path: str,
    value: Any,
    delete: bool,
) -> dict[str, Any]:
    """Validate a declared binding before changing one saved artifact's local viewer."""
    from types import SimpleNamespace

    from clio_agent.gact.a2ui_producer.inspect import declared_controls
    from clio_agent.gact.a2ui_visual import VisualFeedbackError
    from clio_agent.gact.dashboard_reports import read_dashboard_report

    try:
        saved = SimpleNamespace(**read_dashboard_report(app, sid, artifact_id)["surface"])
        if revision is None or saved.id != surface_id or saved.revision != revision:
            return refusal(
                "a2ui_view_stale",
                detail="Inspect the pinned dashboard's surface and revision first.",
            )
        if delete or not viewer_id or view_revision is None:
            return refusal(
                "a2ui_view_control_invalid",
                detail="Saved view controls require a replacement value and inspected viewer id/epoch.",
            )
        controls = declared_controls(
            app, saved.catalog_id, _common.current_surface_components(saved)
        )
        if not any(control["path"] == path for control in controls):
            available = ", ".join(sorted({str(c["path"])[:256] for c in controls})[:12])
            return refusal(
                "a2ui_view_control_unbound",
                detail="This path is not a declared binding in the saved dashboard.",
                hint=(
                    "Use one exact path from inspect_a2ui_surface.controls. Parent-object "
                    "replacement is not supported for saved views. Available paths: "
                    + (available or "none")
                ),
            )
        return app.state.a2ui_visual.control(
            sid,
            surface_id,
            revision,
            artifact_id=artifact_id,
            viewer_id=viewer_id,
            view_revision=view_revision,
            path=path,
            value=value,
        )
    except (ValueError, VisualFeedbackError) as exc:
        return refusal(getattr(exc, "reason", "a2ui_view_control_unavailable"), detail=str(exc))
