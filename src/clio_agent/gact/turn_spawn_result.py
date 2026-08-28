"""Result projection helpers for completed child-agent turns."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from fastapi import FastAPI


def child_workflow_state(app: "FastAPI", child_sid: str, final: Any) -> dict[str, Any]:
    """Return the workflow state carried by a child result, or an empty mapping."""

    final_metadata = getattr(final, "metadata", {}) or {}
    workflow_state = final_metadata.get("workflow_state")
    if isinstance(workflow_state, dict):
        return workflow_state
    child_session = app.state.sessions.get(child_sid)
    session_metadata = getattr(child_session, "metadata", {}) or {}
    workflow_state = session_metadata.get("workflow_state")
    return workflow_state if isinstance(workflow_state, dict) else {}
