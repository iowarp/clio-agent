"""Audit a private client sign-in request without performing account authorization."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any

from fastapi import HTTPException

from clio_agent.gact.a2ui_actions.record import ActionRecord, persist_transition

if TYPE_CHECKING:
    from fastapi import FastAPI


def deliver_source_login(
    app: FastAPI,
    session_id: str,
    record: ActionRecord,
    context: Mapping[str, Any],
    *,
    publish: Callable[..., Any],
) -> ActionRecord:
    """Record an in-owner user click; only the client's existing private UI signs in."""
    session = app.state.sessions.get(session_id)
    service = getattr(app.state, "connected_storage", None)
    allowed = {f"data_source/login/{p}" for p in ("github", "google_drive", "globus")}
    valid = (
        record.action_name in allowed
        and service is not None
        and session is not None
        and set(context) == {"clio_id", "workspace_id"}
        and context.get("clio_id") == service.store.clio_id
        and context.get("workspace_id") == session.workspace_id
    )
    result = record.transition(
        state="consumed" if valid else "failed",
        delivery="client_ui" if valid else "rejected",
        reason="" if valid else "a2ui_source_login_out_of_scope",
        http_status=200 if valid else 422,
    )
    persist_transition(app, result)
    publish(app, session_id, result)
    if not valid:
        raise HTTPException(
            422,
            detail={
                "code": result.reason,
                "message": "Sign-in action belongs to another CLIO or workspace, or is unsupported.",
            },
        )
    return result
