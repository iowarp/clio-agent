"""Read-only agent handoff to trusted, owner-bound connected-data setup."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from clio_agent.gact import context
from clio_agent.gact.agents.tool_instrumentation import native_tool


def connected_data_status() -> dict[str, Any]:
    """Show the trusted Connect data action and read approved source status.

    The user selects folders and signs in privately in the connected-data UI.
    This tool cannot authorize access, accept credentials, or change sources.
    Call it again after setup to obtain approved source references and local
    materialization paths. Do not poll; wait for the user to finish setup.
    """
    app = context.active_app()
    sid = context.active_session_id()
    if app is None or not sid:
        raise ValueError("Connected-data setup requires an active CLIO workspace session")
    session = app.state.sessions.get(sid)
    service = getattr(app.state, "connected_storage", None)
    if session is None or service is None:
        raise ValueError("Connected-data setup requires an active CLIO workspace session")
    workspace = app.state.workspaces.get(session.workspace_id)
    if workspace is None:
        raise ValueError("The session workspace is unavailable on this CLIO")
    sources = []
    for record in service.sources(session.workspace_id):
        source = record.source
        # Never serialize private records, authentication objects, configuration,
        # callbacks or upstream locators into an observed tool result.
        if source.owner.clio_id != service.store.clio_id:
            continue
        sources.append(
            {
                "id": source.id,
                "provider": source.provider,
                "label": source.label,
                "mode": source.mode,
                "connected": record.connected,
                "materialization": source.materialization,
                "revision": source.revision,
                "local_path": source.local_path,
                "owner": source.owner.model_dump(mode="json"),
            }
        )
    return {
        "status": "setup_available",
        "clio_id": service.store.clio_id,
        "workspace_id": session.workspace_id,
        "sources": sources,
    }


def setup_presentation(args: Mapping[str, Any], result: Any, structured: Any) -> dict[str, Any]:
    """Offer a native setup link without any sign-in URL or credential payload."""
    row = structured if isinstance(structured, Mapping) else result
    if not isinstance(row, Mapping) or row.get("status") != "setup_available":
        return {"summary": "Connected data is unavailable", "blocks": []}
    return {
        "summary": "Choose data for this workspace",
        "blocks": [
            {
                "id": "connected-data",
                "type": "link",
                "target": "connected_data",
                "uri": row["clio_id"],
                "workspace_id": row["workspace_id"],
                "label": "Connect data",
            }
        ],
    }


def build_connected_data_status_tool() -> Any:
    """Build the credential-free setup/status tool for the main session."""
    return native_tool(
        connected_data_status,
        name="connected_data_status",
        desc=connected_data_status.__doc__,
        args={},
        presentation=setup_presentation,
        domain="resources",
        title="Connected data",
        read_only=True,
    )
