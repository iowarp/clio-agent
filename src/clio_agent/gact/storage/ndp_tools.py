"""Agent discovery through the same NDP catalog owner as the source picker."""

from __future__ import annotations

from typing import Any

from clio_agent.gact import context
from clio_agent.gact.agents.tool_instrumentation import native_tool
from clio_agent.gact.storage.ndp_presentations import ndp_presentation
from clio_agent.gact.storage.ndp_tile_coverage import tile_coverage_presentation
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
    query: str = "",
    organization: str = "",
    endpoint_id: str = "",
    offset: int = 0,
    dataset_id: str = "",
    resource_offset: int = 0,
    resource_format: str = "",
) -> dict[str, Any]:
    """Find NDP datasets and resources without downloading or connecting them.

    Use connected_data_ndp_organizations to discover organization IDs such as
    earthscope_consortium. organization filters that catalog publisher; it does
    not join an organization or grant its protected Endpoint access. query
    matches catalog metadata with AND between words. Use a few distinctive words,
    not a whole question or synonym list; retry zero matches with fewer words
    before concluding a collection is absent. Returns 50 rows per page, resource IDs and paths,
    never credentials or download URLs. Each row lists its first 20 resources and
    resource_count; when there are more, resource_formats tallies them all and
    next_resource_offset is set. To list more of one dataset, call again with its
    dataset_id (and endpoint_id) and resource_offset: 200 resources per page, no
    new catalog query. For raster tiles over a region, connected_data_ndp_tiles_covering
    picks the covering ones directly. resource_format keeps datasets with at least one
    resource of that format as the record states it (GeoTIFF, CSV, LAZ, Esri REST);
    records often leave format blank, so a dataset without a match may still hold the
    data: each resource also carries name_suffix and its own description. To attach selected resources, call
    connected_data_connect(provider="ndp", root=dataset.id, label=dataset.title,
    configuration={"endpoint_id": dataset.endpoint_id, "resource_ids": [ids]}).
    endpoint_id selects a catalog from connected_data_status.ndp_catalogs; omit
    it for central discovery. A saved ndpep_ connection is accepted only when
    it matches an already configured catalog destination and authentication.
    """
    if (
        offset < 0
        or resource_offset < 0
        or len(query) > 1000
        or len(organization) > 512
        or len(resource_format) > 64
    ):
        raise ValueError("Use nonnegative offsets and a bounded catalog query")
    service = _service()
    if dataset_id:
        catalog = _catalog_id(service, endpoint_id) if endpoint_id else ""
        return service.ndp.resource_page(dataset_id, catalog, resource_offset)
    return service.ndp.search(
        query,
        _catalog_id(service, endpoint_id),
        organization=organization,
        offset=offset,
        limit=50,
        resource_format=resource_format,
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


def connected_data_ndp_tiles_covering(
    dataset_id: str,
    west: float,
    south: float,
    east: float,
    north: float,
    endpoint_id: str = "",
    header_offset: int = 0,
) -> dict[str, Any]:
    """Find which raster tiles of one searched NDP dataset cover a region, before attaching.

    For multi-tile datasets (OpenTopography, USGS 3DEP DEMs) whose catalog
    record does not say where each tile lies. Give the region in WGS84 degrees.
    Reads the dataset's own VRT mosaic when it has one, otherwise each tile's
    raster header; no tile body is downloaded. Returns covering[].id values to
    pass as resource_ids to connected_data_connect, plus tiles outside the
    region and undetermined tiles with reasons. When next_header_offset is set,
    call again with it to classify the remaining tiles. The dataset must come
    from an earlier connected_data_search_ndp result.
    """
    from clio_agent.gact.storage.ndp_tile_coverage import tiles_covering  # noqa: PLC0415

    service = _service()
    catalog = _catalog_id(service, endpoint_id) if endpoint_id else ""
    dataset = service.ndp.observed(dataset_id, catalog)
    return tiles_covering(dataset, (west, south, east, north), header_offset=header_offset)


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
        native_tool(
            connected_data_ndp_tiles_covering,
            name="connected_data_ndp_tiles_covering",
            desc=connected_data_ndp_tiles_covering.__doc__,
            args={
                "dataset_id": {"type": "string"},
                "west": {"type": "number"},
                "south": {"type": "number"},
                "east": {"type": "number"},
                "north": {"type": "number"},
                "endpoint_id": {"type": "string"},
                "header_offset": {"type": "integer"},
            },
            title="NDP tiles covering a region",
            domain="resources",
            read_only=True,
            presentation=tile_coverage_presentation,
        ),
    ]
