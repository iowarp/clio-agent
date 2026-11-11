"""Workspace activation for the optional NDP agent tool surface."""

from __future__ import annotations

from clio_agent.gact import context


def session_ndp_tools_enabled() -> bool:
    """Enable NDP Endpoint operation schemas only for an owned, active source here.

    Catalog discovery and Endpoint connection are not gated: an agent opts in by
    claiming them (the ndp plugin). Operations, requests and uploads need an NDP
    dataset or saved Endpoint in this workspace. Read local connection records on
    each turn build. Account sign-in, catalog configuration, retained downloads and
    other workspaces do not activate them. No authentication, upstream discovery or
    network requests are performed.
    """
    app, sid = context.active_app(), context.active_session_id()
    session = app.state.sessions.get(sid) if app is not None and sid else None
    service = getattr(app.state, "connected_storage", None) if app is not None else None
    if session is None or service is None:
        return False
    if any(
        record.connected
        and record.source.provider == "ndp"
        and record.source.owner.clio_id == service.store.clio_id
        for record in service.sources(session.workspace_id)
    ):
        return True
    return bool(service.ndp_endpoints.connections(session.workspace_id))
