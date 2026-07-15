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
import time
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
    # Attached to the gateway object (not a module map keyed by id(gw)): it
    # dies with the gateway, id-reuse cannot alias a stale registry, and a
    # second build over the same base MERGES instead of overwriting.
    registry: dict[str, Any] = getattr(gw, "_clio_namespace_proxies", {})
    specs_registry: dict[str, MCPServerSpec] = getattr(gw, "_clio_namespace_specs", {})

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
            registry[name] = proxy
            specs_registry[name] = spec
        except Exception as exc:  # noqa: BLE001 - non-fatal: log + skip a bad server
            logger.warning("failed to mount declared MCP %r: %s", name, exc)
    gw._clio_namespace_proxies = registry  # type: ignore[attr-defined]
    gw._clio_namespace_specs = specs_registry  # type: ignore[attr-defined]
    return gw


def namespace_proxies(gw: FastMCP) -> dict[str, Any]:
    """The declared-server proxies mounted on ``gw``, keyed by namespace (#932).

    Lets an executor route a namespaced call straight at ONE mounted proxy
    instead of the composite (whose disabled-tool/hash fallbacks can list —
    and therefore spawn — every mount).
    """

    return dict(getattr(gw, "_clio_namespace_proxies", {}))


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


def list_tool_definitions(gw: FastMCP) -> dict[str, Any]:
    """One transient SEQUENTIAL listing pass: ``{tool_name: MCPTool}``.

    Each namespace lists in its OWN event loop, one at a time — the chain is
    reaped (loop exit kills stdio children) before the next namespace spawns,
    so at most ONE declared chain exists during boot. The previous composite
    pass (one ``Client(gw)`` over every mount) held the WHOLE fleet alive at
    once: the boot memory peak was the sum of all chains at the sampler's
    unluckiest tick — high-variance and budget-breaking (#942; observed
    1.27–1.50 GB across identical-code runs, caught by the v0.8.0 release
    gate).

    Coverage is complete by construction: the in-process built-ins list via
    their server objects (no subprocess), and every declared mount is in the
    ``_clio_namespace_proxies`` registry (``build_gateway`` is the only mount
    site). A namespace whose listing fails degrades to no-tools with a typed
    warning — the same semantics the composite's swallowed-mount aggregation
    had, now with OUR reason attached. Feed the result to BOTH
    ``build_tool_catalog(tools=...)`` and the executors' ``preloaded_tools``
    so boot pays exactly one pass and executors never re-list (#932).
    """

    from clio_agent.tools import listing_cache  # noqa: PLC0415

    specs = namespace_specs(gw)
    tools: dict[str, Any] = {}
    builtins: list[tuple[str, Any]] = [("fs", fs_server), ("shell", shell_server)]
    declared = list(namespace_proxies(gw).items())
    for namespace, server in (*builtins, *declared):
        spec = specs.get(namespace)
        cacheable = spec is not None and spec.transport == "stdio" and bool(spec.command)
        listed: list[Any] | None = None
        if cacheable and spec is not None:
            listed = listing_cache.load_listing(
                namespace, spec.command, tuple(spec.args), spec.env
            )
        live = listed is None
        if live:
            before = _descendant_pids()
            try:
                listed = _list_tools_sync(server)
            except Exception as exc:  # noqa: BLE001 - typed namespace degrade, never a boot crash
                logger.warning(
                    "tool_listing_failed namespace=%s reason=%s (namespace degrades to no tools)",
                    namespace,
                    exc,
                )
                _await_spawned_exit(before)
                continue
            # The chain must be DEAD before the next namespace spawns — loop
            # exit kills stdio children, but termination is asynchronous and
            # an overlapping corpse still holds its RSS.
            _await_spawned_exit(before)
            if cacheable and spec is not None:
                listing_cache.store_listing(
                    namespace, spec.command, tuple(spec.args), listed, spec.env
                )
        assert listed is not None
        for tool in listed:
            prefixed = f"{namespace}_{tool.name}"
            # Direct per-namespace listing yields BARE names; the composite
            # prefixed them with the mount namespace. Consumers key on AND
            # read ``tool.name`` (catalog rows), so the object is renamed too.
            tools[prefixed] = tool.model_copy(update={"name": prefixed})
    return tools


def _descendant_pids() -> frozenset[int]:
    """Snapshot of this process's recursive descendant pids."""

    import psutil  # noqa: PLC0415

    try:
        return frozenset(p.pid for p in psutil.Process().children(recursive=True))
    except Exception as exc:  # noqa: BLE001 - typed: a failed snapshot weakens the
        # one-chain-at-a-time bound (pre-existing children look "spawned", or a
        # mid-wait failure ends the wait early) — say so instead of silence.
        logger.warning("descendant snapshot failed (%s); listing serialization weakened", exc)
        return frozenset()


def _await_spawned_exit(before: frozenset[int], timeout: float = 10.0) -> None:
    """Block until descendants spawned since ``before`` have exited (#942).

    Bounds the boot listing to one live chain at a time. On timeout the pass
    continues with a typed warning — a wedged chain must not hang boot.
    A provider rebind reconstructs the agent while sessions may run: a
    concurrent shell command or lazy fleet spawn can land in the diff and
    cause a bounded bogus wait (≤10s, nothing is killed) — accepted.
    """

    import psutil  # noqa: PLC0415

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        now = _descendant_pids()
        lingering = now - before
        if not lingering:
            return
        # conhost windows tear down on their own schedule; don't wait on them.
        real = []
        for pid in lingering:
            try:
                if psutil.Process(pid).name().lower() != "conhost.exe":
                    real.append(pid)
            except psutil.Error:
                continue
        if not real:
            return
        time.sleep(0.05)
    logger.warning(
        "boot listing chain did not exit within %.0fs; continuing (pids=%s)",
        timeout,
        sorted(lingering),
    )


def namespace_specs(gw: FastMCP) -> dict[str, MCPServerSpec]:
    """The declared-server specs mounted on ``gw``, keyed by namespace (#942)."""

    return dict(getattr(gw, "_clio_namespace_specs", {}))


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
    tools: list[Any] | None = None,
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
    listed = tools if tools is not None else _list_tools_sync(declared_gateway)
    for tool in listed:
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
