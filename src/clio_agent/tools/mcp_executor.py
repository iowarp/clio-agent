"""Event-loop-native MCP execution, timeouts, and result projection."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import threading
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, Protocol, cast
from urllib.parse import urlsplit

from clio_agent.tools.launcher_cache_lock import (
    aacquire_launcher_cache_lock,
    uses_shared_launcher_cache,
)
from clio_agent.tools.mcp_client_lifecycle import close_mcp_context
from clio_agent.tools.mcp_client_lifecycle import log_close_failure as _log_close_failure
from clio_agent.tools.mcp_connection_era import (
    MCPConnectionEra,
    classify_connection_era,
    latest_mcp_connection_era,
    resolved_connect_mode,
)
from clio_agent.tools.mcp_errors import typed_mcp_call_error, typed_mcp_protocol_error
from clio_agent.tools.mcp_namespace_executor import AsyncNamespacePreparationMixin
from clio_agent.tools.mcp_result_projection import (
    bounded_model_tool_result as _bounded_model_tool_result,
)
from clio_agent.tools.mcp_result_text import result_to_text as _result_to_text
from clio_agent.tools.mcp_runtime import enter_connection, make_mcp_client
from clio_agent.tools.mcp_task_routing import (
    record_namespace_route_decision,
    resolve_and_build_direct_client,
)
from clio_agent.tools.mcp_timeout_budget import component_declared_timeout_seconds
from clio_agent.tools.tool_ui_metadata import (
    _tool_visible_to_model as _tool_visible_to_model,  # re-exported for execution.py
)

logger = logging.getLogger(__name__)


class MCPClientProtocol(Protocol):
    """Subset of FastMCP client methods used by the bridge."""

    async def __aenter__(self) -> "MCPClientProtocol":
        """Enter the client context."""
        ...

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> bool | None:
        """Exit the client context."""
        ...

    async def list_tools(self) -> list[Any]:
        """List tools exposed by the backing server."""
        ...

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        progress_handler: Callable[[float, float | None, str | None], Awaitable[None]]
        | None = None,
    ) -> Any:
        """Call a tool and forward optional native progress notifications."""
        ...

    async def read_resource(self, uri: str) -> Any:
        """Read a resource from the backing server."""
        ...


ClientFactory = Callable[[Any], MCPClientProtocol]


@dataclass(frozen=True, repr=False)
class _MCPCallOutcome:
    """Private dual projection of one MCP call.

    ``model_text`` is the legacy result consumed by the agent. ``raw_result`` is
    retained long enough to derive a sanitized public telemetry projection and
    to feed the private MCP Apps observer. ``repr=False`` prevents accidental
    diagnostic logging from serializing private result metadata.
    """

    model_text: str
    raw_result: Any
    source_namespace: str | None


# Call health limits come from server declarations; core supplies no hardcoded overrides.
DEFAULT_TOOL_TIMEOUTS: dict[str, float] = {}
SYNC_TOOL_RESULT_GRACE_SECONDS = 1.0


#: Backwards-compatible alias: the protocol-refusal mapping now lives in the shared
#: :mod:`clio_agent.tools.mcp_errors` seam every direct call path applies (#1114).
_typed_mcp_protocol_error = typed_mcp_protocol_error


@dataclass(frozen=True)
class _ToolTimeoutBudget:
    """One call's timeout (``None`` == unbounded commitment, #1225) + provenance."""

    seconds: float | None
    explicitly_declared: bool


class UncertainMutatingToolOutcomeError(RuntimeError):
    """An inline mutation timed out without a confirmed outcome.

    The CLI executor fences identical retries until its owner is reconstructed.
    """

    def __init__(self, tool: str, timeout_seconds: float, *, retry_blocked: bool) -> None:
        phase = "prior uncertain timeout blocks this retry" if retry_blocked else "call timed out"
        super().__init__(
            "UncertainMutatingToolOutcomeError("
            f"tool={tool!r}, status='outcome_unknown', timeout_seconds={timeout_seconds:g}, "
            "retry_safe=False, action='do_not_retry', "
            f"message={phase!r}; no durable result was received; query durable status or "
            "reconcile the remote system before any new mutation)"
        )


def _mapping_value(value: Any) -> Mapping[str, Any] | None:
    """Return a mapping projection for MCP/Pydantic metadata values."""

    if isinstance(value, Mapping):
        return value
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        projected = dump(by_alias=True, exclude_none=True)
        if isinstance(projected, Mapping):
            return projected
    return None


def _tool_input_schema(tool: Any) -> Mapping[str, Any]:
    """Return one MCP tool's input schema without guessing from call arguments."""

    # Prefer FastMCP 4/MCP 2's input_schema; its deprecated alias can raise.
    # CamelCase is only a fallback for historical objects lacking the modern field.
    if isinstance(tool, Mapping):
        schema = tool.get("input_schema") or tool.get("inputSchema")
    elif hasattr(tool, "input_schema"):
        schema = tool.input_schema
    else:
        schema = getattr(tool, "inputSchema", None)
    return _mapping_value(schema) or {}


def _tool_annotations(tool: Any) -> Mapping[str, Any]:
    """Return protocol-alias MCP annotations for retry-safety decisions."""

    annotations = getattr(tool, "annotations", None)
    if annotations is None and isinstance(tool, Mapping):
        annotations = tool.get("annotations")
    return _mapping_value(annotations) or {}


def _explicit_tool_timeout_seconds(tool: Any, args: Mapping[str, Any]) -> float | None:
    """Compatibility entry point for the shared schema-derived budget calculation."""
    from clio_agent.tools.mcp_timeout_budget import explicit_tool_timeout_seconds

    return explicit_tool_timeout_seconds(
        _mapping_value(_tool_input_schema(tool).get("properties")) or {}, args
    )


def _is_wait_for_terminal_commitment(tool: Any, args: Mapping[str, Any]) -> bool:
    """True iff the TOOL'S OWN schema declares wait_for_terminal AND it's set (#1225)."""
    properties = _mapping_value(_tool_input_schema(tool).get("properties")) or {}
    return "wait_for_terminal" in properties and args.get("wait_for_terminal") is True


def _tool_timeout_is_retry_safe(tool: Any) -> bool:
    """Return whether MCP annotations make a timed-out call safe to repeat."""

    annotations = _tool_annotations(tool)
    return annotations.get("readOnlyHint") is True or annotations.get("idempotentHint") is True


def _tool_call_fingerprint(name: str, args: Mapping[str, Any]) -> str:
    """Return a stable, non-secret-bearing identity for one attempted tool call."""

    encoded = json.dumps(
        {"tool": name, "args": dict(args)},
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _clean_tool_timeouts(tool_timeouts: Mapping[str, float] | None) -> dict[str, float]:
    """Return validated per-tool timeouts merged with built-in long-tool defaults."""

    cleaned = dict(DEFAULT_TOOL_TIMEOUTS)
    if tool_timeouts:
        cleaned.update({str(name): float(timeout) for name, timeout in tool_timeouts.items()})
    invalid = {name: timeout for name, timeout in cleaned.items() if timeout <= 0}
    if invalid:
        raise ValueError(f"tool timeouts must be positive: {sorted(invalid)}")
    return cleaned


def _builtin_namespace_server(namespace: str) -> Any:
    """CLIO's in-process server for a built-in namespace (``fs``, ``shell``), else None.

    A built-in call sent to the composite gateway was resolved by listing every mounted
    server, which starts each declared server not yet running: a session's first
    ``fs_list_directory`` waited about 2.5 s on the ``web`` server's start (X1,
    2026-11; a 2 s server took a first call to 3.7 s in a probe). Declared namespaces
    already get a client of their own; the built-ins now do too.
    """
    if namespace == "fs":
        from clio_agent.tools.servers.fs_server import fs_server  # noqa: PLC0415

        return fs_server
    if namespace == "shell":
        from clio_agent.tools.servers.shell_server import shell_server  # noqa: PLC0415

        return shell_server
    return None


class AsyncMCPToolExecutor(AsyncNamespacePreparationMixin):
    """Async FastMCP execution boundary with no background thread.

    This is the API-service path: it binds a FastMCP client to the caller's
    event loop and exposes explicit async startup, tool calls, and shutdown.
    """

    def __init__(
        self,
        server: Any,
        timeout: float = 30.0,
        tool_timeouts: Mapping[str, float] | None = None,
        client_factory: ClientFactory | None = None,
        preloaded_tools: Mapping[str, Any] | None = None,
        namespace_servers: Mapping[str, Any] | None = None,
        server_id: str = "",
    ) -> None:
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        cleaned_tool_timeouts = _clean_tool_timeouts(tool_timeouts)

        self._server = server
        # #1201: identity label for the primary connection's era record below.
        self._server_id = server_id
        # Route namespaced calls lazily to one backend; keep the composite fallback.
        self._namespace_servers = dict(namespace_servers) if namespace_servers else {}
        self._namespace_clients: dict[str, Any] = {}
        self._close_requested = False
        self._namespace_ctxs: dict[str, Any] = {}
        # Seed direct factories from the gateway; namespace merging extends this registry.
        self._clio_namespace_direct_factories: dict[str, Any] = dict(
            getattr(server, "_clio_namespace_direct_factories", None) or {}
        )
        # Cached route evidence permits healing only from proxy to direct.
        self._namespace_direct_routes: dict[str, bool] = {}
        # #1281 F12 (adversarial review): namespaces whose heal has already
        # been ATTEMPTED -- bounds a namespace to at most one evict+reconnect
        # cycle when a direct-capable verdict's factory is missing/failing,
        # so a permanently-stuck namespace never repeats the cycle (and its
        # false "healed" report) on every subsequent call.
        self._namespace_heal_attempted: set[str] = set()
        # Namespaces whose FIRST routed call succeeded (#934 spawn-diet hooks).
        self._connected_namespaces: set[str] = set()
        self._timeout = timeout
        self._tool_timeouts = cleaned_tool_timeouts
        self._uncertain_mutating_timeouts: dict[str, float] = {}
        self._uncertain_mutating_timeouts_lock = threading.Lock()
        self._client_factory = cast(ClientFactory, client_factory or make_mcp_client)
        self._client_ctx: MCPClientProtocol | None = None
        self._client: MCPClientProtocol | None = None
        self._preloaded_tools = dict(preloaded_tools) if preloaded_tools is not None else None
        self._mcp_tools: dict[str, Any] = {}
        # One lock per namespace, held only while that namespace's client is
        # resolved / connected / healed -- never across a tool call, so calls
        # (a step's concurrent calls, parent + children) run in parallel.
        self._namespace_locks: dict[str, asyncio.Lock] = {}
        self._started = False
        self._closed = False
        # #1201: per-server runtime record of the negotiated protocol era.
        self._connection_era: MCPConnectionEra | None = None
        self._namespace_connection_eras: dict[str, MCPConnectionEra] = {}

    @property
    def started(self) -> bool:
        """Return whether the executor has discovered tools."""
        return self._started

    @property
    def closed(self) -> bool:
        """Return whether the executor has been closed."""
        return self._closed

    @property
    def connection_era(self) -> MCPConnectionEra | None:
        """The primary connection's classified protocol era; ``None`` pre-start (#1201)."""
        return self._connection_era

    def namespace_connection_era(self, namespace: str) -> MCPConnectionEra | None:
        """A namespace-direct backend's classified era; ``None`` if unconnected (#1201)."""
        return self._namespace_connection_eras.get(namespace)

    def namespaces(self) -> tuple[str, ...]:
        """Declared server namespaces this executor routes to (#1201 gact readers)."""
        return tuple(self._namespace_servers)

    async def __aenter__(self) -> "AsyncMCPToolExecutor":
        """Start the executor in an async context manager."""
        return await self.start()

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        """Close the executor from an async context manager."""
        await self.aclose()

    async def start(self) -> "AsyncMCPToolExecutor":
        """Open the client connection and discover tools."""
        if self._closed:
            raise RuntimeError("AsyncMCPToolExecutor is closed")
        if self._started:
            return self

        client_ctx = self._client_factory(self._server)
        client_entered = False
        try:
            client = await enter_connection(client_ctx)
            client_entered = True
            tools = None if self._preloaded_tools is not None else await client.list_tools()
        except BaseException as exc:
            if client_entered:
                with suppress(Exception):
                    await client_ctx.__aexit__(None, None, None)
            if isinstance(exc, Exception):
                typed_error = _typed_mcp_protocol_error(exc)
                if typed_error is not None:
                    raise typed_error from exc
            raise

        # #932: preloaded definitions skip list_tools, which would eagerly
        # spawn every mounted stdio server. Backends connect lazily per namespace.
        self._client_ctx = client_ctx
        self._client = client
        # #1201: stamp + record the negotiated era (typed downgrade under auto mode).
        self._connection_era = classify_connection_era(
            server_id=self._server_id or "primary",
            protocol_version=getattr(client, "protocol_version", None),
            connect_mode=resolved_connect_mode(),
        )
        self._mcp_tools = (
            dict(self._preloaded_tools)
            if self._preloaded_tools is not None
            else {tool.name: tool for tool in tools or []}
        )
        self._started = True
        return self

    async def call_tool(self, name: str, args: Mapping[str, Any]) -> str:
        """Call an MCP tool on the caller's event loop."""

        outcome = await self.call_tool_result(name, args)
        # Bounded here, in the caller's context (the spill lands in its workspace).
        return _bounded_model_tool_result(outcome.model_text)

    async def call_tool_result(
        self,
        name: str,
        args: Mapping[str, Any],
        *,
        progress_handler: Callable[[float, float | None, str | None], Awaitable[None]]
        | None = None,
    ) -> _MCPCallOutcome:
        """Dispatch one RPC; application-owned SDK clients retain accepted task transports."""
        import contextvars

        import mcp_types

        from clio_agent.gact import context

        try:
            app = context.active_app()
            loop = getattr(getattr(app, "state", None), "mcp_app_loop", None)
            if app is not None and loop is not None:
                captured = contextvars.copy_context()

                async def product_call() -> _MCPCallOutcome:
                    if name in {"bash", "shell_bash"}:
                        value = await app.state.builtin_boundary.native_call(
                            name, dict(args), captured
                        )
                        result = mcp_types.CallToolResult(
                            content=[mcp_types.TextContent(type="text", text=json.dumps(value))],
                            structured_content=value,
                        )
                        return _MCPCallOutcome(json.dumps(value), result, "shell")
                    namespace, _, bare = name.partition("_")
                    proxy = self._namespace_servers.get(namespace)
                    if proxy is None and bare and name in self._mcp_tools:
                        proxy = _builtin_namespace_server(namespace)
                    if proxy is not None:
                        configured, decision = resolve_and_build_direct_client(
                            namespace, self._clio_namespace_direct_factories
                        )
                        record_namespace_route_decision(namespace, decision)
                        if configured is None:
                            configured = self._client_factory(proxy)
                        server, tool = namespace, bare
                        spec = getattr(self, "_clio_namespace_specs", {}).get(namespace, {})
                    else:
                        configured, server, tool = (
                            self._client_factory(self._server),
                            self._server_id or "primary",
                            name,
                        )
                        transport = getattr(configured, "transport", None)
                        if transport is None:
                            raise ValueError("Product Tasks require a native SDK client transport")
                        spec = {"server": server, "transport": type(transport).__name__}
                    auth_scope = hashlib.sha256(
                        json.dumps(spec, sort_keys=True, default=str).encode()
                    ).hexdigest()
                    try:
                        result = await app.state.external_tasks.call(
                            server,
                            configured,
                            tool,
                            dict(args),
                            auth_scope=auth_scope,
                            progress_handler=progress_handler,
                        )
                    except Exception as error:
                        self._namespace_call_failed(namespace if proxy is not None else None, error)
                        raise
                    self._namespace_succeeded(namespace if proxy is not None else None)
                    return _MCPCallOutcome(
                        _result_to_text(result), result, namespace if proxy is not None else None
                    )

                coroutine = product_call()
                if asyncio.get_running_loop() is loop:
                    return await coroutine
                return await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(coroutine, loop))
            # CLI callers retain ordinary SDK inline/input behavior. Product Tasks
            # require the application owner, and never inherit a resident executor.
            from clio_agent.tools.task_call_context import TASK_CALL, begin_backend_call

            forwarded, token = begin_backend_call(self, name, dict(args))
            try:
                previous = self._prior_uncertain_mutating_timeout(name, forwarded)
                if previous is not None:
                    raise UncertainMutatingToolOutcomeError(name, previous, retry_blocked=True)
                budget = self._timeout_budget_for_call(name, forwarded).seconds
                client, tool, namespace = await self._route(name)
                try:
                    async with asyncio.timeout(budget):
                        result = await client.call_tool(
                            tool, forwarded, progress_handler=progress_handler
                        )
                except TimeoutError as error:
                    if not self._tool_timeout_is_retry_safe(name):
                        raise self.mark_uncertain_mutating_timeout(
                            name,
                            forwarded,
                            budget if budget is not None else self._timeout_for_tool(name),
                        ) from error
                    raise TimeoutError(f"MCP tool {name!r} timed out after {budget}s") from error
                except Exception as error:
                    self._namespace_call_failed(namespace, error)
                    raise
                self._namespace_succeeded(namespace)
            finally:
                TASK_CALL.reset(token)
            return _MCPCallOutcome(_result_to_text(result), result, namespace)
        except Exception as error:
            translated = typed_mcp_call_error(error, tool=name)
            if translated is not None:
                raise translated from error
            raise

    async def read_resource(self, namespace: str | None, uri: str) -> Any:
        """Read ``uri`` from exactly ``namespace`` (or the composite gateway).

        MCP App resources are capability-bound to the server that produced the
        originating tool result. The caller supplies that recorded namespace;
        this method never searches or fans out across mounted servers.
        """

        if self._closed:
            raise RuntimeError("AsyncMCPToolExecutor is closed")
        if self._client is None:
            raise RuntimeError("AsyncMCPToolExecutor is not started")
        parsed_uri = urlsplit(uri)
        if not parsed_uri.scheme:
            raise ValueError("MCP resource URI must be absolute")

        if namespace:
            proxy = self._namespace_servers.get(namespace)
            if proxy is None:
                raise ValueError(f"unknown MCP namespace {namespace!r}")
            client = await self._namespace_client(namespace, proxy)
        else:
            client = self._client
        return await asyncio.wait_for(client.read_resource(uri), timeout=self._timeout)

    async def _connect_namespace(self, namespace: str, proxy: Any) -> Any:
        """Connect a declared namespace, retaining era, route and cache-lock evidence."""
        from clio_agent.tools import spawn_diet

        specs: Mapping[str, Any] = getattr(self, "_clio_namespace_specs", None) or {}
        spec = specs.get(namespace)
        direct_factories: Mapping[str, Callable[[], Any]] = (
            getattr(self, "_clio_namespace_direct_factories", None) or {}
        )
        try:
            direct_client, decision = resolve_and_build_direct_client(namespace, direct_factories)
            record_namespace_route_decision(namespace, decision)
            ctx = direct_client if direct_client is not None else self._client_factory(proxy)
            if spec is not None and uses_shared_launcher_cache(spec):
                async with aacquire_launcher_cache_lock(namespace):
                    client = await enter_connection(ctx)
            else:
                client = await enter_connection(ctx)
        except Exception:
            spawn_diet.spawn_failed(namespace)
            raise
        self._namespace_ctxs[namespace] = ctx
        self._namespace_clients[namespace] = client
        self._namespace_direct_routes[namespace] = direct_client is not None
        if direct_client is None:
            self._namespace_connection_eras[namespace] = classify_connection_era(
                server_id=namespace,
                protocol_version=getattr(client, "protocol_version", None),
                connect_mode=resolved_connect_mode(),
            )
        else:
            # The direct client records its era on entry; do not duplicate that audit.
            self._namespace_connection_eras[namespace] = latest_mcp_connection_era(
                namespace
            ) or classify_connection_era(
                server_id=namespace,
                protocol_version=getattr(client, "protocol_version", None),
                connect_mode=resolved_connect_mode(),
            )
        if direct_client is not None:
            self._namespace_succeeded(namespace)
        return client

    async def _route(self, name: str) -> tuple[Any, str, str | None]:
        """Resolve one namespace; reject unknown names before composite discovery."""

        namespace, _, bare = name.partition("_")
        proxy = self._namespace_servers.get(namespace)
        if proxy is None or not bare:
            if self._namespace_servers and name not in self._mcp_tools:
                # A name outside every known tool must NEVER reach the
                # composite: its resolution fallback (fastmcp >= 3.4) can list
                # — and therefore spawn — every mounted backend. Typed error
                # instead (the model hallucinated a tool name).
                raise ValueError(f"unknown tool {name!r}: not in the preloaded tool catalog")
            assert self._client is not None
            return self._client, name, None
        client = await self._namespace_client(namespace, proxy)
        return client, bare, namespace

    def _timeout_for_tool(self, name: str) -> float:
        """Return the effective timeout for a single tool invocation."""

        return self._tool_timeouts.get(name, self._timeout)

    def _timeout_budget_for_call(self, name: str, args: Mapping[str, Any]) -> _ToolTimeoutBudget:
        """Timeout floor of >= any explicit budget; unbounded for a commitment (#1225)."""
        base = self._timeout_for_tool(name)
        configured_explicitly = name in self._tool_timeouts
        tool = self._mcp_tools.get(name)
        declared = _explicit_tool_timeout_seconds(tool, args)
        if declared is not None:
            return _ToolTimeoutBudget(base + declared, explicitly_declared=True)
        if _is_wait_for_terminal_commitment(tool, args):
            return _ToolTimeoutBudget(None, explicitly_declared=True)
        # Retain the component's declared duration floor for ordinary CLI calls.
        properties = _mapping_value(_tool_input_schema(tool).get("properties")) or {}
        component = component_declared_timeout_seconds(properties)
        if component is not None and component > base:
            return _ToolTimeoutBudget(component, explicitly_declared=True)
        return _ToolTimeoutBudget(base, explicitly_declared=configured_explicitly)

    def _tool_timeout_is_retry_safe(self, name: str) -> bool:
        """Return whether protocol annotations permit retry after an unknown timeout."""

        return _tool_timeout_is_retry_safe(self._mcp_tools.get(name))

    def _prior_uncertain_mutating_timeout(
        self,
        name: str,
        args: Mapping[str, Any],
    ) -> float | None:
        """Return a prior uncertain timeout that fences an identical mutation."""

        with self._uncertain_mutating_timeouts_lock:
            return self._uncertain_mutating_timeouts.get(_tool_call_fingerprint(name, args))

    def mark_uncertain_mutating_timeout(
        self,
        name: str,
        args: Mapping[str, Any],
        timeout_seconds: float,
    ) -> UncertainMutatingToolOutcomeError:
        """Fence a non-idempotent call after a timeout with no durable result."""

        with self._uncertain_mutating_timeouts_lock:
            self._uncertain_mutating_timeouts[_tool_call_fingerprint(name, args)] = timeout_seconds
        return UncertainMutatingToolOutcomeError(
            name,
            timeout_seconds,
            retry_blocked=False,
        )

    def get_tool_names(self) -> list[str]:
        """Return model-visible discovered tool names.

        MCP Apps may declare app-only tools. Those remain available through the
        capability-bound app bridge but must not enlarge the model tool surface.
        """

        return [
            name for name, tool in self._mcp_tools.items() if _tool_visible_to_model(name, tool)
        ]

    def get_tool_definitions(self) -> dict[str, Any]:
        """Return model-visible MCP tool definitions keyed by stable name."""

        return {
            name: tool
            for name, tool in self._mcp_tools.items()
            if _tool_visible_to_model(name, tool)
        }

    def get_all_tool_definitions(self) -> dict[str, Any]:
        """Return all definitions, including capability-bound app-only tools."""

        return dict(self._mcp_tools)

    async def aclose(self, *, force: bool = False) -> None:
        """Close the client connection."""
        if self._closed:
            return
        self._close_requested = True
        self._closed = True

        for namespace, ctx in list(self._namespace_ctxs.items()):
            try:
                await asyncio.wait_for(
                    close_mcp_context(
                        ctx, owns_transport=self._namespace_direct_routes.get(namespace, False)
                    ),
                    timeout=5.0,
                )
            except Exception as exc:  # noqa: BLE001 - teardown continues; typed reason logged
                _log_close_failure(f"namespace client {namespace!r}", exc)
        self._namespace_ctxs.clear()
        self._namespace_clients.clear()
        self._namespace_direct_routes.clear()
        self._namespace_heal_attempted.clear()

        if self._client_ctx is not None:
            close_timeout = min(5.0, max(0.1, self._timeout))
            try:
                await asyncio.wait_for(
                    self._client_ctx.__aexit__(None, None, None),
                    timeout=close_timeout,
                )
            except Exception as exc:  # noqa: BLE001 - teardown continues; typed reason logged
                _log_close_failure("executor client", exc)

        self._client = None
        self._client_ctx = None
        self._namespace_locks.clear()


__all__ = [
    "AsyncMCPToolExecutor",
    "ClientFactory",
    "MCPClientProtocol",
    "UncertainMutatingToolOutcomeError",
]
