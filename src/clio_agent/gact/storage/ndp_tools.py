"""Agent discovery through the same NDP catalog owner as the source picker."""

from __future__ import annotations

from typing import Any

from clio_agent.gact import context
from clio_agent.gact.agents.tool_instrumentation import native_tool
from clio_agent.gact.storage.ndp_presentations import ndp_presentation
from clio_agent.gact.storage.service import StorageService


def _service() -> StorageService:
    app, sid = context.active_app(), context.active_session_id()
    session = app.state.sessions.get(sid) if app is not None and sid else None
    if app is None or session is None or app.state.workspaces.get(session.workspace_id) is None:
        raise ValueError("NDP discovery requires an active workspace conversation")
    return app.state.connected_storage


def _catalog_id(service: StorageService, identifier: str) -> str:
    """Resolve an owned saved connection only to an already configured catalog."""
    if not identifier.startswith("ndpep_"):
        return identifier
    app, sid = context.active_app(), context.active_session_id()
    session = app.state.sessions.get(sid) if app is not None and sid else None
    if session is None:
        raise ValueError("NDP discovery requires an active workspace conversation")
    connection = service.ndp_endpoints.get(session.workspace_id, identifier)
    if connection.hidden:
        raise ValueError("This saved Endpoint is hidden; choose a visible NDP catalog")
    for endpoint in service.ndp.endpoints():
        if (
            not connection.ssh_profile
            and connection.ssh_route is None
            and connection.url == endpoint.url
            and (connection.authentication == "ndp") == endpoint.authenticated
        ):
            return endpoint.id
    raise ValueError(
        "This saved service is not a configured catalog. Use an ndp_catalogs id from "
        "connected_data_status (or omit endpoint_id for central discovery); use "
        "Endpoint operations for this service's local catalog."
    )


def connected_data_search_ndp(
    query: str = "", organization: str = "", endpoint_id: str = "", offset: int = 0
) -> dict[str, Any]:
    """Find NDP datasets and resources without downloading or connecting them.

    Use connected_data_ndp_organizations to discover organization IDs such as
    earthscope_consortium. organization filters that catalog publisher; it does
    not join an organization or grant its protected Endpoint access. query
    matches catalog metadata with AND between words. Use a few distinctive words,
    not a whole question or synonym list; retry zero matches with fewer words
    before concluding a collection is absent. Returns 50 rows per page, resource IDs and paths,
    never credentials or download URLs. To attach selected resources, call
    connected_data_connect(provider="ndp", root=dataset.id, label=dataset.title,
    configuration={"endpoint_id": dataset.endpoint_id, "resource_ids": [ids]}).
    endpoint_id selects a catalog from connected_data_status.ndp_catalogs; omit
    it for central discovery. A saved ndpep_ connection is accepted only when
    it matches an already configured catalog destination and authentication.
    """
    if offset < 0 or len(query) > 1000 or len(organization) > 512:
        raise ValueError("Use a nonnegative offset and a bounded catalog query")
    service = _service()
    return service.ndp.search(
        query, _catalog_id(service, endpoint_id), organization=organization, offset=offset, limit=50
    )


def connected_data_ndp_organizations(query: str = "", endpoint_id: str = "") -> dict[str, Any]:
    """Find NDP catalog publisher IDs, for example EarthScope, without account setup.

    Pass a returned id and endpoint_id to connected_data_search_ndp. This is
    discovery only: catalog organizations and protected Endpoint role groups
    are different. Account sign-in is obtained through connected_data_status's
    private NDP login_action when a selected resource requires it.
    endpoint_id selects a catalog from connected_data_status.ndp_catalogs; omit
    it for central discovery. A saved ndpep_ connection works only when it matches
    a configured catalog. For another service's local organizations, discover
    and invoke its organization operation with the Endpoint tools.
    """
    if len(query) > 1000:
        raise ValueError("Use a catalog organization query of at most 1000 characters")
    service = _service()
    return service.ndp.organizations(query, _catalog_id(service, endpoint_id))


def build_ndp_discovery_tools() -> list[Any]:
    """Expose credential-free discovery to the root source-connection workflow."""
    return [
        native_tool(
            connected_data_search_ndp,
            name="connected_data_search_ndp",
            desc=connected_data_search_ndp.__doc__,
            args={
                "query": {"type": "string"},
                "organization": {"type": "string"},
                "endpoint_id": {"type": "string"},
                "offset": {"type": "integer"},
            },
            title="Search NDP",
            domain="resources",
            read_only=True,
            presentation=ndp_presentation,
        ),
        native_tool(
            connected_data_ndp_organizations,
            name="connected_data_ndp_organizations",
            desc=connected_data_ndp_organizations.__doc__,
            args={"query": {"type": "string"}, "endpoint_id": {"type": "string"}},
            title="NDP organizations",
            domain="resources",
            read_only=True,
            presentation=ndp_presentation,
        ),
    ]
