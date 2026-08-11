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
import functools
import inspect
import logging
import time
from collections.abc import Callable, Iterable, Mapping
from contextlib import suppress
from typing import TYPE_CHECKING, Any

from fastmcp import Client, FastMCP
from fastmcp.server.providers.proxy import FastMCPProxy

if TYPE_CHECKING:
    from clio_agent.tools.jarvis_jobs import JarvisJobs
    from clio_agent.tools.mcp_runtime import MCPClientCapabilities, MCPClientHandlers
    from clio_agent.tools.remote_mcp import RemoteMcpFederation

from clio_agent.tools.catalog import (
    TOOL_CATALOG,
    ToolCatalogEntry,
    classification_tags,
    normalize_mcp_annotations,
)
from clio_agent.tools.mcp_config import (
    BUILTIN_SERVER_NAMES,
    MCPServerSpec,
    MCPSpawnError,
    transport_for,
)
from clio_agent.tools.servers.fs_server import fs_server
from clio_agent.tools.servers.shell_server import shell_server

logger = logging.getLogger(__name__)


def _mount_with_namespace(parent: FastMCP, server: FastMCP, namespace: str) -> None:
    """Mount a server with a stable namespaced tool name prefix."""
    parent.mount(server, namespace=namespace)


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


def _proxy_for_spec(
    spec: MCPServerSpec,
    cwd: str | None = None,
    *,
    handlers: "MCPClientHandlers | None" = None,
    capabilities: "MCPClientCapabilities | None" = None,
) -> FastMCP:
    """Build a lazy FastMCP proxy backed by a Client over the spec's transport.

    The proxy connects lazily on first ``list_tools``/``call_tool``, so an
    unreachable declared server only degrades to "that namespace has no tools"
    rather than failing gateway construction.

    This is an EXECUTION path: the proxy's backend client dispatches ``call_tool``
    to the declared server. BOTH the handler-populated and no-handler cases build
    ONE :class:`fastmcp.server.providers.proxy.ProxyClient` base through
    :func:`make_mcp_client` (#1106/#1111) — the single site that stamps CLIO's
    ``clientInfo`` identity, installs any hook dispatchers (#1106), and installs
    any declared ``capabilities`` (#1111). Using ``ProxyClient`` for BOTH cases is
    load-bearing: it preserves FastMCP's ``_ForwardingClientSession`` and
    ``forward_incoming_headers=True`` — so caller authorization is forwarded to
    HTTP backends, unhandled sampling / roots / log requests are push-forwarded to
    the front, and backend results are relayed (not re-validated) mid-proxy. A hook
    that CLIO wires overrides only that one handler; the rest keep forwarding. The
    capability ``session_class`` subclasses the proxy's forwarding session, so
    forwarding is COMPOSED, never discarded. Earlier the no-handler branch handed
    the raw transport to ``create_proxy`` (leaking ``mcp/0.1.0`` downstream) and
    the handler branch built a plain ``Client`` (discarding all of the above).

    The base is built once and cloned per request via ``.new()`` — carrying the
    session kwargs, hook dispatchers, and ``_transport_options`` (capability
    ``session_class``) onto each clone — with ``_mirror_front_era_mode`` applied to
    the clone (mode + ``backend_mode``) so the whole chain speaks one protocol era
    end-to-end (proven by ``test_gateway_mirrors_front_era_to_backend``). This
    base-once / clone-per-request pattern is required for correct era mirroring: a
    fresh per-call construction reuses the transport's kept-alive session and leaks
    the first request's era onto later requests. Constructing the base opens no
    connection; a subprocess spawns only when a clone connects.

    #1201 (adversarial review, PR #1202): THIS clone -- the one ``_client_factory``
    returns -- is the ONE seam that dials the real backend transport for real.
    ``base_backend`` itself is never entered (only its clones are), so era
    instrumentation is applied fresh to ``fresh`` on every call, never to
    ``base_backend`` (a live probe, ``scripts/diagnostics/
    probe_1201_era_detectability.py``, proved the executor's own front-leg
    capture on the composite gateway is BLIND here: ``_mirror_front_era_mode``
    just above forces this SAME clone's mode to match the front's
    already-negotiated, always-modern era before it ever connects for real).

    ``cwd`` (when given) is passed to stdio transports so the subprocess is
    spawned in that working directory; http transports ignore it.
    """
    transport = transport_for(spec, cwd=cwd)

    from dataclasses import replace  # noqa: PLC0415

    from fastmcp.server.providers.proxy import ProxyClient, _mirror_front_era_mode  # noqa: PLC0415

    from clio_agent.tools.mcp_connection_era import instrument_client_era  # noqa: PLC0415
    from clio_agent.tools.mcp_runtime import make_mcp_client  # noqa: PLC0415

    base_backend = make_mcp_client(
        transport, handlers=handlers, capabilities=capabilities, client_cls=ProxyClient
    )

    def _client_factory() -> Any:
        fresh = base_backend.new()
        mode = _mirror_front_era_mode()
        if mode is not None:
            fresh.mode = mode
            fresh._transport_options = replace(fresh._transport_options, backend_mode=mode)
        # Applied to THIS clone: base_backend is never entered directly (only
        # its .new() clones are, per-request), so this is where the one REAL
        # backend connect this factory ever produces actually gets classified.
        return instrument_client_era(fresh, server_id=spec.name)

    return FastMCPProxy(client_factory=_client_factory)


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
    handlers: "MCPClientHandlers | None" = None,
    capabilities: "MCPClientCapabilities | None" = None,
    remote_mcp_federation: "RemoteMcpFederation | None" = None,
    jarvis_jobs: "JarvisJobs | None" = None,
    relay_status: Mapping[str, Any] | None = None,
) -> FastMCP:
    """Build the agent's tool gateway: built-ins PLUS the declared MCP servers.

    Each usable declared spec is mounted under its name as namespace via a
    lazy FastMCP proxy whose backend is built through ``make_mcp_client`` (so it
    carries CLIO identity), preserving the ``<name>_<tool>`` naming. The universal
    built-ins (``fs``/``shell``) are always present; a declared server may not
    shadow a built-in namespace.

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
        handlers: Optional execution-path handler bundle (#1106) forwarded to the
            default proxy factory so each declared server's backend client carries
            the CLIO dispatchers. Ignored when ``proxy_factory`` is supplied.
        capabilities: Optional client-capability declaration (#1111) forwarded to
            the default proxy factory so each declared server's backend advertises
            it. Ignored when ``proxy_factory`` is supplied.
        remote_mcp_federation: Optional relay catalog snapshot projected under the
            reserved ``remote`` namespace. Its bare server names are mounted back
            into the exact relay aliases ``remote_<ns>_<tool>``.

        jarvis_jobs: Optional curated durable application surface mounted under the
            reserved jarvis namespace.
        relay_status: Optional typed production wiring status. A non-empty reason is
            retained on the gateway for diagnostics even when only part of the relay
            surface could be mounted.

    Returns:
        The gateway with the built-ins and declared proxies mounted.

    Notes:
        Mounting only constructs lazy proxies; no subprocess is spawned and no
        connection is opened here. Connection failures surface later as an empty
        namespace, never as a startup crash.
    """
    gw = base_gateway if base_gateway is not None else _new_base_gateway()
    if proxy_factory is not None:
        make_proxy = proxy_factory
    elif handlers is not None or capabilities is not None:
        # Bind the execution-path handler bundle / capability declaration onto the
        # default factory so proxy backend clients carry them (#1106/#1111).
        make_proxy = functools.partial(
            _proxy_for_spec, handlers=handlers, capabilities=capabilities
        )
    else:
        make_proxy = _proxy_for_spec
    accepts_cwd = _proxy_factory_accepts_cwd(make_proxy)
    # Attached to the gateway object (not a module map keyed by id(gw)): it
    # dies with the gateway, id-reuse cannot alias a stale registry, and a
    # second build over the same base MERGES instead of overwriting.
    registry: dict[str, Any] = getattr(gw, "_clio_namespace_proxies", {})
    specs_registry: dict[str, MCPServerSpec] = getattr(gw, "_clio_namespace_specs", {})
    degraded: dict[str, dict[str, Any]] = getattr(gw, "_clio_degraded_capabilities", {})

    reason = str((relay_status or {}).get("reason") or "")
    relay_details = (relay_status or {}).get("details")
    if remote_mcp_federation is None and jarvis_jobs is None and not reason:
        reason = "relay_tools_not_configured"
    if reason:
        definitions = {
            "relay_tools_not_configured": {
                "category": "relay_configuration",
                "description": "Relay-backed remote MCP and JARVIS tools are not configured.",
                "recovery_actions": ["configure_relay"],
            },
            "relay_catalog_discovery_failed": {
                "category": "relay_connectivity",
                "description": "Relay catalog discovery failed during agent construction.",
                "recovery_actions": ["check_relay_service", "retry_agent_construction"],
            },
        }
        definition = definitions.get(
            reason,
            {
                "category": "relay_configuration",
                "description": "Relay-backed tools are unavailable.",
                "recovery_actions": ["configure_relay"],
            },
        )
        degraded["relay"] = {
            "reason": reason,
            **definition,
            **({"details": dict(relay_details)} if isinstance(relay_details, Mapping) else {}),
        }
        logger.warning("relay tool surface degraded reason=%s", reason)
    else:
        degraded.pop("relay", None)

    if remote_mcp_federation is not None:
        from clio_agent.tools.remote_mcp import (  # noqa: PLC0415
            RELAY_FOLLOW_NAMESPACE,
            REMOTE_MCP_NAMESPACE,
        )

        occupied = _mounted_namespaces(gw) | set(BUILTIN_SERVER_NAMES)
        federation_namespaces = {REMOTE_MCP_NAMESPACE}
        if remote_mcp_federation.catalog.follow_tools:
            federation_namespaces.add(RELAY_FOLLOW_NAMESPACE)
        collision = occupied & federation_namespaces
        if collision:
            raise ValueError("remote MCP federation namespace is already provided")
        remote_server = remote_mcp_federation.server
        _mount_with_namespace(gw, remote_server, REMOTE_MCP_NAMESPACE)
        registry[REMOTE_MCP_NAMESPACE] = remote_server
        if remote_mcp_federation.catalog.follow_tools:
            follow_server = remote_mcp_federation.follow_server
            _mount_with_namespace(gw, follow_server, RELAY_FOLLOW_NAMESPACE)
            registry[RELAY_FOLLOW_NAMESPACE] = follow_server

    if jarvis_jobs is not None:
        from clio_agent.tools.jarvis_jobs import JARVIS_NAMESPACE  # noqa: PLC0415

        occupied = _mounted_namespaces(gw) | set(BUILTIN_SERVER_NAMES)
        if JARVIS_NAMESPACE in occupied:
            raise ValueError("curated JARVIS jobs namespace is already provided")
        jarvis_server = jarvis_jobs.server
        _mount_with_namespace(gw, jarvis_server, JARVIS_NAMESPACE)
        registry[JARVIS_NAMESPACE] = jarvis_server

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
    gw._clio_degraded_capabilities = degraded  # type: ignore[attr-defined]
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
            listed = listing_cache.load_listing(namespace, spec.command, tuple(spec.args), spec.env)
        live = listed is None
        if live:
            before = _descendant_pids()
            try:
                # Declared servers list through a LISTING-OWNED transport
                # (finding 1); only the in-process built-ins use the shared
                # server object (they carry no transport).
                if spec is None:
                    listed = _list_tools_sync(server)
                else:
                    try:
                        listed = _list_declared_tools(spec)
                    except MCPSpawnError as spawn_exc:
                        # The spec cannot yield a listing-owned transport: an
                        # injected in-process proxy (tests) whose backend has NO
                        # transport to poison, or a misconfigured launcher whose
                        # mounted proxy will re-confirm the degrade below. Only
                        # then do we touch the mounted server. Logged so the
                        # fallback is never silent.
                        logger.debug(
                            "namespace=%s: spec transport unavailable (%s); "
                            "listing via the mounted proxy",
                            namespace,
                            spawn_exc,
                        )
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


def _run_coro_sync(factory: Callable[[], Any]) -> Any:
    """Run a coroutine to completion on a fresh loop, safe inside or outside one.

    ``factory`` is a zero-arg callable returning a fresh coroutine each time it is
    invoked (a coroutine object cannot be awaited twice, and the running-loop
    branch re-creates it on the pool thread).
    """

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(factory())
    with concurrent.futures.ThreadPoolExecutor() as pool:
        return pool.submit(lambda: asyncio.run(factory())).result()


def _list_tools_sync(gw: FastMCP) -> list[Any]:
    """List an IN-PROCESS server's tools synchronously (no declared transports).

    Used only for in-process FastMCP servers (the ``fs``/``shell`` built-ins and
    the built-ins-only base gateway): those carry no client transport, so a
    throwaway listing loop cannot strand a kept-alive session or spawn a
    subprocess. Declared (proxy-backed) servers MUST list through
    :func:`_list_declared_tools`, which owns its transport exclusively.
    """

    async def _list() -> list[Any]:
        async with Client(gw) as client:
            return await client.list_tools()

    return _run_coro_sync(_list)


def _list_declared_tools(spec: MCPServerSpec) -> list[Any]:
    """List one declared server's BARE tools via a LISTING-OWNED transport.

    Exclusive ownership (finding 1): the listing builds its OWN transport from the
    spec and tears it down in a ``finally`` on the same short-lived loop, so it
    never connects the shared proxy transport the long-lived executor reuses.
    Two properties follow directly:

    * A catalog refresh can never disconnect an in-flight executor call — the two
      run on disjoint transport instances and disjoint subprocesses.
    * No cross-loop poisoning: fastmcp-4 pins a kept-alive ``ClientSession`` to
      the loop that opened it, but a listing-owned transport is fully torn down
      before its loop closes, so nothing loop-bound survives.
    """

    async def _list() -> list[Any]:
        client = Client(transport_for(spec))
        try:
            async with client:
                return await client.list_tools()
        finally:
            # Listing-owned: force the transport down on THIS loop instead of
            # leaning on stdio ``keep_alive`` (fastmcp-4 default), which would
            # otherwise strand a loop-bound session and leave the subprocess up.
            disconnect = getattr(client.transport, "disconnect", None)
            if disconnect is not None:
                with suppress(Exception):
                    await disconnect()

    return _run_coro_sync(_list)


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


def _tool_annotations(tool: Any) -> Mapping[str, Any] | None:
    """Return a JSON-compatible MCP annotation mapping from a listed tool, or ``None``.

    FastMCP exposes ``annotations`` as an ``mcp.types.ToolAnnotations`` model on listed tools,
    while tests/persisted rows use plain mappings. Normalizes both to a mapping so the catalog
    can project the read/write tag from the SAME annotations the permission gate consults for
    external MCP calls (#1061). Unknown shapes → ``None`` (fail-safe: classified as effectful).
    """

    return normalize_mcp_annotations(tool)


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
    # Exclusive ownership (finding 1): when tools are not supplied, derive them
    # through ``list_tool_definitions`` — a per-namespace listing over
    # LISTING-OWNED transports — never a composite ``Client(gateway)`` pass that
    # would connect (and, pre-fix, poison) the shared proxy transports.
    listed = tools if tools is not None else list(list_tool_definitions(declared_gateway).values())
    for tool in listed:
        name = tool.name
        if name in merged:
            continue
        namespace = _namespace_of(name)
        if namespace in BUILTIN_SERVER_NAMES:
            # Built-in namespaces are owned by the static catalog; skip any
            # built-in tool not explicitly listed there.
            continue
        # Read/write classification is a PROJECTION of the tool's declared MCP annotations
        # (#1061), the SAME source of truth the built-in catalog and the permission gate use —
        # so external MCP tools classify read-only/write uniformly with the built-ins.
        tags = _tool_tags(tool) | {namespace} | classification_tags(_tool_annotations(tool))
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
    # Exclusive ownership (finding 1): enumerate through the per-namespace,
    # LISTING-OWNED primitive rather than a composite ``Client(gateway)`` pass
    # that would connect the shared proxy transports.
    tools = list(list_tool_definitions(target).values())
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
    # Exclusive ownership (finding 4): route through the per-namespace,
    # LISTING-OWNED primitive so introspecting a declared gateway on this
    # (possibly short-lived) loop never connects — and cannot strand a
    # kept-alive session on — the shared proxy transports the executor reuses.
    # Offloaded to a worker thread because ``list_tool_definitions`` is a
    # blocking, own-loop listing pass.
    definitions = await asyncio.to_thread(list_tool_definitions, target)
    return [
        {
            "name": t.name,
            "description": t.description,
            # fastmcp-4: ``Tool.inputSchema`` -> ``Tool.input_schema``.
            "input_schema": getattr(t, "input_schema", None),
            "server": _namespace_of(t.name),
        }
        for t in definitions.values()
    ]
