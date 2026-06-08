"""
CLIO Agent MCP Gateway

FastMCP gateway that composes all tool servers under namespaced prefixes.
Mounts HDF5 and Parquet servers with namespaced tool access.

The gateway exposes tools namespaced as:
    hdf5_list_datasets, hdf5_analyze_dataset, hdf5_check_compression,
    hdf5_optimize_chunking, hdf5_analyze_file,
    parquet_analyze_schema, parquet_query_data, parquet_compute_statistics

Usage:
    >>> from clio_agent.tools.gateway import gateway, get_gateway
    >>> # Use with FastMCP Client
    >>> from fastmcp import Client
    >>> async with Client(gateway) as client:
    ...     tools = await client.list_tools()
    ...     result = await client.call_tool("hdf5_analyze_file", {"filepath": "data.h5"})
    ...     result = await client.call_tool("parquet_analyze_schema", {"filepath": "data.parquet"})
"""

import asyncio
import concurrent.futures
import inspect
import logging
import os
from collections.abc import Callable, Mapping
from typing import Any, cast

from fastmcp import Client, FastMCP

from clio_agent.tools.catalog import TOOL_CATALOG, ToolCatalogEntry
from clio_agent.tools.mcp_config import MCPServerSpec, transport_for
from clio_agent.tools.servers.adios_server import adios_server
from clio_agent.tools.servers.fs_server import fs_server
from clio_agent.tools.servers.geospatial_server import geospatial_server
from clio_agent.tools.servers.hdf5_server import hdf5_server
from clio_agent.tools.servers.ndp_server import ndp_server
from clio_agent.tools.servers.parquet_server import parquet_server
from clio_agent.tools.servers.sac_server import sac_server
from clio_agent.tools.servers.shell_server import shell_server
from clio_agent.tools.servers.terrain_server import terrain_server

logger = logging.getLogger(__name__)

# Feature flag: opt-in declaration-driven MCP sourcing. DEFAULT OFF means the
# in-process gateway + static catalog behave byte-for-byte as before. Only when
# this flag is truthy does ``build_gateway``/``build_tool_catalog`` mount and
# enumerate declared MCP servers additively on top of the in-process gateway.
DECLARED_MCPS_FLAG = "CLIO_DECLARED_MCPS"


def declared_mcps_enabled(env: Mapping[str, str] | None = None) -> bool:
    """Return whether declaration-driven MCP sourcing is enabled via env flag."""
    source = os.environ if env is None else env
    value = str(source.get(DECLARED_MCPS_FLAG, "")).strip().lower()
    return value in {"1", "true", "yes", "on"}


def _mount_with_namespace(parent: FastMCP, server: FastMCP, namespace: str) -> None:
    """Mount a server with stable namespaced tool names across FastMCP versions."""
    mount_params = inspect.signature(parent.mount).parameters
    mount = cast(Any, parent.mount)
    if "namespace" in mount_params:
        mount(server, namespace=namespace)
    else:
        mount(server, prefix=namespace)


# Gateway singleton: composes all tool servers under namespaced prefixes.
gateway = FastMCP("clio-gateway")
_mount_with_namespace(gateway, hdf5_server, "hdf5")
_mount_with_namespace(gateway, parquet_server, "parquet")
_mount_with_namespace(gateway, adios_server, "adios")
_mount_with_namespace(gateway, ndp_server, "ndp")
_mount_with_namespace(gateway, sac_server, "sac")
_mount_with_namespace(gateway, geospatial_server, "geospatial")
_mount_with_namespace(gateway, terrain_server, "terrain")
_mount_with_namespace(gateway, fs_server, "fs")
_mount_with_namespace(gateway, shell_server, "shell")


def get_gateway() -> FastMCP:
    """Return the CLIO gateway instance.

    Returns:
        The singleton FastMCP gateway with all tool servers mounted.
    """
    return gateway


def _proxy_for_spec(spec: MCPServerSpec) -> FastMCP:
    """Build a lazy FastMCP proxy backed by a Client over the spec's transport.

    The proxy connects lazily on first ``list_tools``/``call_tool``, so an
    unreachable declared server only degrades to "that namespace has no tools"
    rather than failing gateway construction.
    """
    import warnings  # noqa: PLC0415

    with warnings.catch_warnings():
        # FastMCP 3.2 deprecates as_proxy in favor of create_proxy; both are the
        # same machinery and the spec is written against as_proxy. Silence the
        # deprecation here so a successful mount stays quiet.
        warnings.simplefilter("ignore")
        return FastMCP.as_proxy(Client(transport_for(spec)))


def build_gateway(
    declared_specs: Mapping[str, MCPServerSpec],
    *,
    base_gateway: FastMCP | None = None,
    proxy_factory: Callable[[MCPServerSpec], FastMCP] | None = None,
) -> FastMCP:
    """Mount declared MCP servers as proxies additively on top of the in-process gateway.

    Each usable declared spec is mounted under its name as namespace via a
    FastMCP proxy (``FastMCP.as_proxy(Client(transport_for(spec)))``), preserving
    the existing ``<name>_<tool>`` naming. This is purely additive: the in-process
    servers already mounted on ``base_gateway`` are never removed or unmounted.

    Args:
        declared_specs: ``name -> MCPServerSpec`` declarations to mount. Specs
            with validation errors, or whose name collides with a built-in /
            already-mounted in-process namespace, are skipped (logged).
        base_gateway: The gateway to mount onto. Defaults to the in-process
            singleton ``gateway``. Mounting mutates this gateway in place and
            returns it.
        proxy_factory: Optional override that turns a spec into a FastMCP proxy.
            Defaults to ``_proxy_for_spec`` (a lazy ``Client`` over the spec's
            transport). Tests inject an in-process proxy here so no subprocess is
            spawned; production never passes this.

    Returns:
        The (mutated) base gateway with declared proxies additively mounted.

    Notes:
        Mounting only constructs lazy proxies; no subprocess is spawned and no
        connection is opened here. Connection failures surface later as an empty
        namespace, never as a startup crash.
    """
    gw = base_gateway if base_gateway is not None else gateway
    make_proxy = proxy_factory or _proxy_for_spec

    # Names already provided in-process must not be shadowed/unmounted.
    existing = _mounted_namespaces(gw)
    for name, spec in declared_specs.items():
        if not spec.usable:
            logger.warning(
                "skipping declared MCP %r (%s): %s",
                name,
                spec.source or "unknown source",
                "; ".join(spec.validation_errors),
            )
            continue
        if name in existing:
            logger.warning("skipping declared MCP %r: namespace already provided in-process", name)
            continue
        try:
            proxy = make_proxy(spec)
            _mount_with_namespace(gw, proxy, name)
            existing.add(name)
        except Exception as exc:  # noqa: BLE001 - non-fatal: log + skip a bad server
            logger.warning("failed to mount declared MCP %r: %s", name, exc)
    return gw


def _mounted_namespaces(gw: FastMCP) -> set[str]:
    """Namespaces already present on a gateway, derived from its live tool names.

    This is version-independent: it reads the namespace prefix off each currently
    exposed ``<namespace>_<tool>`` name rather than poking FastMCP internals. Used
    only for collision avoidance so a declared server never shadows an in-process
    namespace (additive mounts never unmount anything regardless).
    """
    try:
        tools = _list_tools_sync(gw)
    except Exception:  # noqa: BLE001 - collision check is best-effort
        return set()
    return {_namespace_of(tool.name) for tool in tools}


def _list_tools_sync(gw: FastMCP) -> list[Any]:
    """List a gateway's tools synchronously, safe inside or outside an event loop."""

    async def _list() -> list[Any]:
        async with Client(gw) as client:
            return await client.list_tools()

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(_list())
    with concurrent.futures.ThreadPoolExecutor() as pool:
        return pool.submit(lambda: asyncio.run(_list())).result()


def _namespace_of(tool_name: str) -> str:
    """Derive the owning server namespace from a namespaced tool name."""
    return tool_name.split("_", 1)[0] if "_" in tool_name else tool_name


def _tool_tags(tool: Any) -> frozenset[str]:
    """Extract MCP tool tags (FastMCP stores them under meta['fastmcp']['tags'])."""
    meta = getattr(tool, "meta", None)
    if isinstance(meta, Mapping):
        fastmcp_meta = meta.get("fastmcp")
        if isinstance(fastmcp_meta, Mapping):
            tags = fastmcp_meta.get("tags")
            if isinstance(tags, (list, tuple, set, frozenset)):
                return frozenset(str(t) for t in tags)
    return frozenset()


def build_tool_catalog(
    declared_gateway: FastMCP | None = None,
    *,
    static_catalog: Mapping[str, ToolCatalogEntry] | None = None,
) -> dict[str, ToolCatalogEntry]:
    """Merge the static catalog with entries derived from declared-MCP namespaces.

    For each tool exposed by ``declared_gateway`` that is not already in the
    static catalog, synthesize a ``ToolCatalogEntry`` whose owner/namespace is the
    tool-name prefix and whose tags are the MCP tool's tags plus the namespace.
    Static catalog entries always win (they are merged on top), so default
    behavior is unchanged when there is nothing new to add.

    Args:
        declared_gateway: A gateway whose declared-MCP tools should be enumerated.
            When ``None``, the catalog equals the static catalog (a copy).
        static_catalog: The base catalog to merge onto. Defaults to ``TOOL_CATALOG``.

    Returns:
        A merged ``name -> ToolCatalogEntry`` dict.
    """
    base = TOOL_CATALOG if static_catalog is None else static_catalog
    merged: dict[str, ToolCatalogEntry] = dict(base)
    if declared_gateway is None:
        return merged

    for tool in _list_tools_sync(declared_gateway):
        name = tool.name
        if name in merged:
            continue
        namespace = _namespace_of(name)
        tags = _tool_tags(tool) | {namespace}
        merged[name] = ToolCatalogEntry(
            name=name,
            owner=namespace,
            tags=frozenset(tags),
            visible_to=frozenset({namespace}),
            planner_visible=True,
        )
    return merged


def list_capabilities() -> list[dict[str, str]]:
    """Return lightweight tool capability summaries for context compilation.

    Returns compact tool summaries (~400 tokens total) instead of full schemas
    (~47K tokens). Each entry contains tool name, first sentence of description,
    and server prefix. Designed for injection into compiled context prompts
    without blowing token budgets.

    Returns:
        List of dicts with name, description (first sentence), and server keys.

    Example:
        >>> caps = list_capabilities()
        >>> for c in caps:
        ...     print(f"{c['server']}/{c['name']}: {c['description']}")
    """
    import asyncio

    async def _list():
        async with Client(gateway) as client:
            return await client.list_tools()

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        tools = asyncio.run(_list())
    else:
        # If already in an event loop, create a new one in a thread.
        # Avoid calling asyncio.run(_list()) first: that creates a
        # coroutine object before asyncio.run raises, which leaks an
        # unawaited-coroutine warning even though the fallback succeeds.
        import concurrent.futures

        with concurrent.futures.ThreadPoolExecutor() as pool:
            tools = pool.submit(lambda: asyncio.run(_list())).result()

    capabilities = []
    for t in sorted(tools, key=lambda x: x.name):
        # Extract first sentence of description
        desc = t.description or ""
        first_sentence = desc.split(".")[0].strip() + "." if desc else ""
        server = _infer_tool_server(t.name)
        capabilities.append(
            {
                "name": t.name,
                "description": first_sentence,
                "server": server,
            }
        )
    return capabilities


def _infer_tool_server(tool_name: str) -> str:
    """Infer the bundled gateway server from CLIO's tool-name prefix."""

    if tool_name.startswith("hdf5_"):
        return "hdf5"
    if tool_name.startswith("parquet_"):
        return "parquet"
    if tool_name.startswith("adios_"):
        return "adios"
    if tool_name.startswith("ndp_"):
        return "ndp"
    if tool_name.startswith("sac_"):
        return "sac"
    if tool_name.startswith("geospatial_"):
        return "geospatial"
    if tool_name.startswith("hpc_"):
        return "hpc"
    if tool_name.startswith("format_"):
        return "format"
    if tool_name.startswith("genomics_"):
        return "genomics"
    if tool_name.startswith("imaging_"):
        return "imaging"
    if tool_name.startswith("mass_spec_"):
        return "mass_spec"
    if tool_name.startswith("materials_"):
        return "materials"
    if tool_name.startswith("terrain_"):
        return "terrain"
    if tool_name.startswith("fs_"):
        return "fs"
    if tool_name.startswith("shell_"):
        return "shell"
    return "unknown"


async def list_gateway_tools() -> list[dict[str, Any]]:
    """List all tools available through the gateway with their metadata.

    Useful for debugging and introspection. Returns tool names, descriptions,
    and input schemas from all mounted servers.

    Returns:
        List of dicts with name, description, and input_schema for each tool.

    Example:
        >>> import asyncio
        >>> tools = asyncio.run(list_gateway_tools())
        >>> for t in tools:
        ...     print(f"{t['name']}: {t['description']}")
    """
    async with Client(gateway) as client:
        mcp_tools = await client.list_tools()
        return [
            {
                "name": t.name,
                "description": t.description,
                "input_schema": t.inputSchema,
                "server": _infer_tool_server(t.name),
            }
            for t in mcp_tools
        ]
