"""Session projections for GACT 0.3."""

from __future__ import annotations

from typing import Any, Mapping

from clio_agent.gact.protocol.v3 import utcnow_iso
from clio_agent.gact.session_defaults import session_effort

_SESSION_STATE = {
    "idle": "completed",
    "running": "running",
    "waiting_permission": "waiting_permission",
    "waiting_user": "waiting_user",
    "error": "failed",
    "cancelled": "cancelled",
}


def session_to_v3(session: Any) -> dict[str, Any]:
    """Project the 0.2 session record into the normalized 0.3 shape."""

    model = getattr(session, "model", {})
    if not isinstance(model, Mapping):
        model = {}
    metadata = getattr(session, "metadata", {})
    if not isinstance(metadata, Mapping):
        metadata = {}
    agent = getattr(session, "agent", {})
    if isinstance(agent, Mapping):
        agent_id = agent.get("id")
    else:
        agent_id = getattr(agent, "id", "")
    status = str(getattr(session, "status", "") or "idle")
    message_count = max(0, int(getattr(session, "message_count", 0) or 0))
    row: dict[str, Any] = {
        "id": str(getattr(session, "id", "") or ""),
        "workspace_id": str(getattr(session, "workspace_id", "") or ""),
        "title": str(getattr(session, "title", "") or "Untitled session"),
        "state": _SESSION_STATE.get(status, "interrupted"),
        "created_at": str(getattr(session, "created_at", "") or utcnow_iso()),
        "updated_at": str(getattr(session, "updated_at", "") or utcnow_iso()),
        "last_interaction_at": str(
            getattr(session, "last_interaction_at", "")
            or getattr(session, "created_at", "")
            or utcnow_iso()
        ),
        "message_count": message_count,
        "pinned": bool(metadata.get("pinned", False)),
        "archived": bool(getattr(session, "archived", False)),
    }
    # Usage rollup — absent (not even null) until the session has actually
    # exchanged a message, matching this projection's missing-vs-null
    # convention. Once populated, cost_usd is null (not 0.0) whenever no turn
    # ever produced a real cost source (provider report or price-table match)
    # — see SessionRecord.cost_known / turn_usage.roll_up_usage. A client must
    # not read null as "free"; it means "unknown."
    if message_count:
        row["tokens_input"] = max(0, int(getattr(session, "tokens_input", 0) or 0))
        row["tokens_output"] = max(0, int(getattr(session, "tokens_output", 0) or 0))
        row["cost_usd"] = (
            float(getattr(session, "cost_usd", 0.0) or 0.0)
            if bool(getattr(session, "cost_known", False))
            else None
        )
    optional = {
        "provider_id": model.get("provider_id"),
        "model_id": model.get("model_id"),
        "effort": session_effort(metadata),
        "branch": metadata.get("branch") or metadata.get("git_branch"),
        "parent_session_id": getattr(session, "parent_session_id", ""),
        "agent_id": agent_id,
        "active_blueprint_id": metadata.get("active_agent_blueprint_id"),
        "active_blueprint_name": metadata.get("active_agent_blueprint_name"),
        "active_blueprint_version": metadata.get("active_agent_blueprint_version"),
        "active_blueprint_scope": metadata.get("active_agent_blueprint_scope"),
    }
    row.update({key: str(value) for key, value in optional.items() if value})
    row.update(
        {
            "mode": str(getattr(session, "mode", "edit") or "edit"),
            "edit_mode": str(getattr(session, "edit_mode", "diff") or "diff"),
            "routing_mode": str(getattr(session, "routing_mode", "auto") or "auto"),
            "approval_mode": str(getattr(session, "approval_mode", "ask") or "ask"),
        }
    )
    return row
