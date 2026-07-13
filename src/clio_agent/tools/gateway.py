"""CLIO Agent MCP Gateway.

The gateway composes tool servers under namespaced prefixes
(``<namespace>_<tool>``). Core ships only the universal in-process built-ins
(``fs``/``shell``); every domain/case tool is a **declared MCP server** that the
active blueprint/pack brings in (see ``tools/mcp_config.py``). ``build_gateway``
proxy-mounts those declared servers next to the built-ins, and
``build_tool_catalog`` derives the tool catalog from the connected namespaces
merged with the static built-in entries.

Usage:
    >>> from clio_agent.tools.gateway import build_gateway
    >>> from clio_agent.tools.mcp_config import load_mcp_servers
    >>> gw = build_gateway(load_mcp_servers(pack_servers=...))
    >>> from fastmcp import Client
    >>> async with Client(gw) as client:
    ...     tools = await client.list_tools()
"""

import asyncio
import concurrent.futures
import inspect
import logging
from collections.abc import Callable, Iterable, Mapping
from typing import Any, cast

from fastmcp import Client, FastMCP

from clio_agent.tools.catalog import TOOL_CATALOG, ToolCatalogEntry
from clio_agent.tools.mcp_config import BUILTIN_SERVER_NAMES, MCPServerSpec, transport_for
from clio_agent.tools.servers.fs_server import fs_server
from clio_agent.tools.servers.shell_server import shell_server

logger = logging.getLogger(__name__)


def _mount_with_namespace(parent: FastMCP, server: FastMCP, namespace: str) -> None:
    """Mount a server with stable namespaced tool names across FastMCP versions."""
    mount_params = inspect.signature(parent.mount).parameters
    mount = cast(Any, parent.mount)
    if "namespace" in mount_params:
        mount(server, namespace=namespace)
    else:
        mount(server, prefix=namespace)


def _mount_builtins(gw: FastMCP) -> None:
    """Mount the universal in-process built-in servers onto a gateway."""
    _mount_with_namespace(gw, fs_server, "fs")
    _mount_with_namespace(gw, shell_server, "shell")


def _new_base_gateway() -> FastMCP:
    """Return a fresh gateway with only the universal built-ins mounted."""
    gw = FastMCP("clio-gateway")
    _mount_builtins(gw)
    return gw


# Gateway singleton: the universal built-ins only. Declared domain servers are
# mounted per agent via ``build_gateway(load_mcp_servers(...))``.
gateway = _new_base_gateway()


def get_gateway() -> FastMCP:
    """Return the CLIO gateway instance (built-ins only)."""
    return gateway


def _proxy_for_spec(spec: MCPServerSpec, cwd: str | None = None) -> FastMCP:
    """Build a lazy FastMCP proxy backed by a Client over the spec's transport.

    The proxy connects lazily on first ``list_tools``/``call_tool``, so an
    unreachable declared server only degrades to "that namespace has no tools"
    rather than failing gateway construction.

    ``cwd`` (when given) is passed to stdio transports so the subprocess is
    spawned in that working directory; http transports ignore it.
    """
    import warnings  # noqa: PLC0415

    with warnings.catch_warnings():
        # FastMCP 3.2 deprecates as_proxy in favor of create_proxy; both are the
        # same machinery. Silence the deprecation so a successful mount stays quiet.
        warnings.simplefilter("ignore")
        return FastMCP.as_proxy(Client(transport_for(spec, cwd=cwd)))


def _proxy_factory_accepts_cwd(factory: Callable[..., FastMCP]) -> bool:
    """Whether a proxy factory accepts a second positional ``cwd`` argument.

    Keeps backward compatibility with test factories that take only the spec.
    """
    try:
        params = inspect.signature(factory).parameters.values()
    except (TypeError, ValueError):
        return False
    positional = [
        p
        for p in params
        if p.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
    ]
    has_varargs = any(p.kind is inspect.Parameter.VAR_POSITIONAL for p in params)
    return has_varargs or len(positional) >= 2


def build_gateway(
    declared_specs: Mapping[str, MCPServerSpec],
    *,
    cwd: str | None = None,
    base_gateway: FastMCP | None = None,
    proxy_factory: Callable[..., FastMCP] | None = None,
) -> FastMCP:
    """Build the agent's tool gateway: built-ins PLUS the declared MCP servers.

    Each usable declared spec is mounted under its name as namespace via a
    FastMCP proxy (``FastMCP.as_proxy(Client(transport_for(spec)))``), preserving
    the ``<name>_<tool>`` naming. The universal built-ins (``fs``/``shell``) are
    always present; a declared server may not shadow a built-in namespace.

    Args:
        declared_specs: ``name -> MCPServerSpec`` declarations to mount. Specs
            with validation errors, or whose name collides with a built-in /
            already-mounted namespace, are skipped (logged).
        cwd: Working directory for stdio MCP subprocesses, so every stdio tool
            writes into that directory by default. ``None`` keeps the spawning
            process's cwd (current behavior). Http (url) servers ignore ``cwd``;
            they stay shared regardless.
        base_gateway: The gateway to mount onto. When ``None`` a fresh gateway
            with only the built-ins mounted is created. When provided, it is
            mounted onto in place and returned (used by tests).
        proxy_factory: Optional override that turns a spec into a FastMCP proxy.
            Defaults to ``_proxy_for_spec`` (a lazy ``Client`` over the spec's
            transport). Called as ``proxy_factory(spec, cwd)`` when it accepts a
            second argument, else ``proxy_factory(spec)`` for compatibility.
            Tests inject an in-process proxy here so no subprocess is spawned;
            production never passes this.

    Returns:
        The gateway with the built-ins and declared proxies mounted.

    Notes:
        Mounting only constructs lazy proxies; no subprocess is spawned and no
        connection is opened here. Connection failures surface later as an empty
        namespace, never as a startup crash.
    """
    gw = base_gateway if base_gateway is not None else _new_base_gateway()
    make_proxy = proxy_factory or _proxy_for_spec
    accepts_cwd = _proxy_factory_accepts_cwd(make_proxy)

    # Names already provided (built-ins / earlier mounts) must not be shadowed.
    existing = _mounted_namespaces(gw) | set(BUILTIN_SERVER_NAMES)
    for name, spec in declared_specs.items():
        if name in BUILTIN_SERVER_NAMES:
            logger.warning("skipping declared MCP %r: reserved built-in namespace", name)
            continue
        if not spec.usable:
            logger.warning(
                "skipping declared MCP %r (%s): %s",
                name,
                spec.source or "unknown source",
                "; ".join(spec.validation_errors),
            )
            continue
        if name in existing:
            logger.warning("skipping declared MCP %r: namespace already provided", name)
            continue
        try:
            # Only stdio specs honor cwd; http specs are shared and ignore it.
            proxy = (
                make_proxy(spec, cwd if spec.transport == "stdio" else None)
                if accepts_cwd
                else make_proxy(spec)
            )
            _mount_with_namespace(gw, proxy, name)
            existing.add(name)
        except Exception as exc:  # noqa: BLE001 - non-fatal: log + skip a bad server
            logger.warning("failed to mount declared MCP %r: %s", name, exc)
    return gw


def _mounted_namespaces(gw: FastMCP) -> set[str]:
    """Namespaces already present on a gateway, derived from its live tool names.

    Version-independent: reads the namespace prefix off each exposed
    ``<namespace>_<tool>`` name rather than poking FastMCP internals.
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
    """Derive the owning server namespace from a namespaced tool name.

    The namespace is everything before the FIRST ``_``. This split is only sound
    because server names are constrained to ``[a-z0-9-]`` (no ``_``) at declaration
    time by ``tools.mcp_config._validate_server_name`` — a name like ``my_server``
    would otherwise mis-derive to ``my``. Keep that invariant if you touch either.
    """
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


def _expert_visibility(experts: Iterable[Any] | None) -> dict[str, set[str]]:
    """Map each declared tool name to the expert ids that list it in ``tools:``.

    A tool is visible to an expert iff that expert lists it. The planner sees a
    tool iff at least one expert that lists it is planner-visible.
    """
    visible: dict[str, set[str]] = {}
    for expert in experts or []:
        expert_id = str(getattr(expert, "id", "") or "").strip()
        if not expert_id:
            continue
        metadata = getattr(expert, "metadata", {}) or {}
        planner_visible = bool(metadata.get("planner_visible", True))
        for tool_name in getattr(expert, "tools", []) or []:
            name = str(tool_name).strip()
            if not name:
                continue
            scopes = visible.setdefault(name, set())
            scopes.add(expert_id)
            if planner_visible:
                scopes.add("planner")
    return visible


def build_tool_catalog(
    declared_gateway: FastMCP | None = None,
    *,
    experts: Iterable[Any] | None = None,
    static_catalog: Mapping[str, ToolCatalogEntry] | None = None,
) -> dict[str, ToolCatalogEntry]:
    """Build the tool catalog from the static built-ins + connected MCP namespaces.

    For each tool exposed by ``declared_gateway`` that is not already a static
    built-in entry, synthesize a ``ToolCatalogEntry`` whose owner/namespace is the
    tool-name prefix and whose tags are the MCP tool's tags plus the namespace.
    Visibility is derived from each expert's ``tools:`` list (``experts``): a tool
    is visible to an expert iff that expert lists it, and planner-visible iff a
    planner-visible expert lists it. The owning namespace is always in
    ``visible_to`` so a tool is at minimum self-scoped.

    Args:
        declared_gateway: A gateway whose tools should be enumerated. When
            ``None``, the catalog equals the static catalog (a copy).
        experts: Loaded pack experts (objects with ``id``, ``tools``, optional
            ``metadata``) used to derive declared-tool visibility.
        static_catalog: The base catalog to merge onto. Defaults to ``TOOL_CATALOG``.

    Returns:
        A merged ``name -> ToolCatalogEntry`` dict. Static built-in entries win.
    """
    base = TOOL_CATALOG if static_catalog is None else static_catalog
    merged: dict[str, ToolCatalogEntry] = dict(base)
    if declared_gateway is None:
        return merged

    visibility = _expert_visibility(experts)
    for tool in _list_tools_sync(declared_gateway):
        name = tool.name
        if name in merged:
            continue
        namespace = _namespace_of(name)
        if namespace in BUILTIN_SERVER_NAMES:
            # Built-in namespaces are owned by the static catalog; skip any
            # built-in tool not explicitly listed there.
            continue
        tags = _tool_tags(tool) | {namespace}
        scopes = set(visibility.get(name, set())) | {namespace}
        merged[name] = ToolCatalogEntry(
            name=name,
            owner=namespace,
            tags=frozenset(tags),
            visible_to=frozenset(scopes),
            planner_visible="planner" in scopes,
        )
    return merged


def list_capabilities(gw: FastMCP | None = None) -> list[dict[str, str]]:
    """Return lightweight tool capability summaries for context compilation.

    Returns compact tool summaries (~400 tokens total) instead of full schemas.
    Each entry contains tool name, first sentence of description, and server
    namespace. Designed for injection into compiled context prompts without
    blowing token budgets.

    Args:
        gw: Gateway to introspect. Defaults to the built-ins-only singleton.

    Returns:
        List of dicts with name, description (first sentence), and server keys.
    """
    target = gw if gw is not None else gateway
    tools = _list_tools_sync(target)
    capabilities = []
    for t in sorted(tools, key=lambda x: x.name):
        desc = t.description or ""
        first_sentence = desc.split(".")[0].strip() + "." if desc else ""
        capabilities.append(
            {
                "name": t.name,
                "description": first_sentence,
                "server": _namespace_of(t.name),
            }
        )
    return capabilities


async def list_gateway_tools(gw: FastMCP | None = None) -> list[dict[str, Any]]:
    """List all tools available through a gateway with their metadata.

    Useful for debugging and introspection. Returns tool names, descriptions,
    and input schemas from all mounted servers.

    Args:
        gw: Gateway to introspect. Defaults to the built-ins-only singleton.

    Returns:
        List of dicts with name, description, input_schema, and server for each tool.
    """
    target = gw if gw is not None else gateway
    async with Client(target) as client:
        mcp_tools = await client.list_tools()
        return [
            {
                "name": t.name,
                "description": t.description,
                "input_schema": t.inputSchema,
                "server": _namespace_of(t.name),
            }
            for t in mcp_tools
        ]
