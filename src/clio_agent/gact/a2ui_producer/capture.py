"""Capture the pixels of one mounted, revision-matched view as native model media."""

from __future__ import annotations

import hashlib
from typing import Any

from clio_agent import paths
from clio_agent.gact import viewed_media
from clio_agent.gact.a2ui import utcnow_iso
from clio_agent.gact.a2ui_producer import _common
from clio_agent.gact.a2ui_producer._refusal import refusal
from clio_agent.gact.a2ui_visual import VisualFeedbackError
from clio_agent.gact.agents.tool_instrumentation import native_tool
from clio_agent.gact.artifacts.minting import (
    _session_workspace_id,
    _workspace_root,
    artifact_name_for_path,
    mint_artifact,
)
from clio_agent.gact.artifacts.records import ArtifactKind, Mechanism
from clio_agent.gact.artifacts.storage import ingest_artifact_identity
from clio_agent.gact.view_image_tool import VIEW_IMAGE_DESCRIPTOR_TYPE
from clio_agent.tools.execution import tool_workspace_context


def build_capture_a2ui_surface_tool() -> Any:
    """Build a session-scoped native media tool, with no implicit answer submission."""

    def capture_a2ui_surface(
        surface_id: str,
        expected_revision: int,
        component_id: str = "",
        viewer_id: str = "",
        expected_view_revision: int | None = None,
        artifact_id: str = "",
        timeout_seconds: float = 15,
    ) -> dict[str, Any]:
        """Inspect real pixels from an open widget or named saved dashboard version.

        First inspect its definition and viewers with inspect_a2ui_surface.
        Pass the expected revision; optionally select a viewer and view epoch.
        component_id captures a displayed component, otherwise the whole view.
        Hidden tabs and absent viewers fail explicitly. A stale view must be re-inspected.
        The result attaches the matching PNG to your next model step and retains an artifact.
        Use it to review framing, labels and visible patterns, then refine declared controls.
        This operation does not answer an ask_user question.
        """
        resolved = _common.active_app_and_session()
        if isinstance(resolved, dict):
            return resolved
        app, sid = resolved
        if artifact_id:
            from clio_agent.gact.dashboard_reports import read_dashboard_report

            try:
                saved = read_dashboard_report(app, sid, artifact_id)["surface"]
                if saved["id"] != surface_id or saved["revision"] != expected_revision:
                    return refusal(
                        "a2ui_view_stale",
                        detail="The requested saved dashboard does not match its surface and revision.",
                    )
            except ValueError as exc:
                return refusal("a2ui_capture_artifact_unavailable", detail=str(exc))
        surface = app.state.a2ui_store.get(sid, surface_id)
        if not artifact_id and (surface is None or surface.state == "deleted"):
            return refusal("a2ui_surface_not_found", detail=f"No live surface: {surface_id}")
        if not artifact_id and surface.revision != expected_revision:
            return refusal(
                "a2ui_view_stale", detail="The definition changed; inspect its current revision."
            )
        workspace_id = _session_workspace_id(app, sid)
        root = _workspace_root(app, workspace_id)
        if root is None:
            return refusal(
                "a2ui_capture_workspace_unavailable",
                detail="This session has no resolved workspace.",
            )
        try:
            data, evidence = app.state.a2ui_visual.capture(
                sid,
                surface_id,
                expected_revision,
                component_id=component_id,
                viewer_id=viewer_id,
                view_revision=expected_view_revision,
                artifact_id=artifact_id,
                timeout=timeout_seconds,
            )
            current = app.state.a2ui_store.get(sid, surface_id)
            if not artifact_id and (current is None or current.revision != expected_revision):
                raise VisualFeedbackError(
                    "a2ui_view_stale", "The definition changed while pixels were captured."
                )
            digest = hashlib.sha256(data).hexdigest()
            target = (
                paths.workspace_state_dir(root) / "a2ui-captures" / f"{evidence['request_id']}.png"
            ).resolve()
            if not target.is_relative_to(paths.workspace_state_dir(root).resolve()):
                raise VisualFeedbackError(
                    "a2ui_capture_path_invalid", "Capture storage escapes the workspace state."
                )
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            ingested = ingest_artifact_identity(app, target, workspace_root=root)
            version = mint_artifact(
                app,
                sid,
                name=artifact_name_for_path(target),
                workspace_id=workspace_id,
                evidence=ingested.evidence,
                kind=ArtifactKind.IMAGE,
                mechanism=Mechanism.HARNESS,
                producer={
                    "designation": "a2ui_visual_capture",
                    "session_id": sid,
                    "captured_at": utcnow_iso(),
                    **evidence,
                },
                custody=ingested.custody,
                path=str(target),
                ingested=ingested,
                not_ingested_size=ingested.not_ingested_size,
            )
            if version is None:
                raise VisualFeedbackError(
                    "a2ui_capture_artifact_failed", "Could not register the captured image."
                )
            with tool_workspace_context(root):
                snapshot, snapshot_hash = viewed_media.snapshot(data, ".png")
            return {
                "type": VIEW_IMAGE_DESCRIPTOR_TYPE,
                "path": str(target),
                "media_type": "image/png",
                "size_bytes": len(data),
                "sha256": digest,
                "snapshot": snapshot,
                "snapshot_sha256": snapshot_hash,
                "artifact_id": version.artifact_id,
                "capture": evidence,
            }
        except (VisualFeedbackError, OSError, ValueError) as exc:
            return refusal(getattr(exc, "reason", "a2ui_capture_failed"), detail=str(exc))

    return native_tool(
        capture_a2ui_surface,
        name="capture_a2ui_surface",
        title="Inspect rendered widget",
        domain="surfaces",
        presentation="text",
        read_only=True,
        desc=capture_a2ui_surface.__doc__,
        args={
            "surface_id": {"type": "string", "description": "Surface id from inspection."},
            "expected_revision": {
                "type": "integer",
                "description": "Exact definition revision to capture.",
            },
            "component_id": {
                "type": "string",
                "description": "Displayed component id, or empty for whole view.",
            },
            "viewer_id": {
                "type": "string",
                "description": "Viewer from inspection, or empty to select one.",
            },
            "expected_view_revision": {
                "type": "integer",
                "description": "Exact optional viewer-state epoch.",
            },
            "artifact_id": {
                "type": "string",
                "description": "Immutable saved dashboard version id, or empty for live surface.",
            },
            "timeout_seconds": {
                "type": "number",
                "description": "Capture budget in seconds, at most 20.",
            },
        },
    )
