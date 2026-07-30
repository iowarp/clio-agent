"""Async MCP tool execution boundary.

This module owns the event-loop-native FastMCP executor, its narrow client
protocol, and the timeout and result-projection helpers required by that
executor.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import threading
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, Protocol, cast
from urllib.parse import urlsplit

from clio_agent.tools import spawn_diet
from clio_agent.tools.mcp_runtime import make_mcp_client

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

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        """Call a named tool on the backing server."""
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


# Per-tool wall-clock timeouts are domain-specific and now come from MCP
# server declarations (a server's ``timeout`` maps into ``tool_timeouts``),
# not from a hardcoded core table. Core ships no default overrides.
DEFAULT_TOOL_TIMEOUTS: dict[str, float] = {}
REPEATED_TRANSIENT_FAILURE_LIMIT = 2
SYNC_TOOL_RESULT_GRACE_SECONDS = 1.0


@dataclass(frozen=True)
class _ToolTimeoutBudget:
    """One invocation's executor timeout and whether the tool explicitly declared it."""

    seconds: float
    explicitly_declared: bool


class UncertainMutatingToolOutcomeError(RuntimeError):
    """Raised when a mutating, non-idempotent tool times out with unknown outcome.

    The remote operation may have crossed its side-effect boundary even though no
    result reached the agent. Retrying such a call can duplicate the mutation, so
    the executor blocks an identical later call until a fresh executor is built.
    """

    def __init__(self, tool: str, timeout_seconds: float, *, retry_blocked: bool) -> None:
        phase = "prior uncertain timeout blocks this retry" if retry_blocked else "call timed out"
        super().__init__(
            "UncertainMutatingToolOutcomeError("
            f"tool={tool!r}, status='outcome_unknown', timeout_seconds={timeout_seconds:g}, "
            "retry_safe=False, executor_work_may_continue=True, action='do_not_retry', "
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

    schema = getattr(tool, "inputSchema", None)
    if schema is None and isinstance(tool, Mapping):
        schema = tool.get("inputSchema") or tool.get("input_schema")
    return _mapping_value(schema) or {}


def _tool_annotations(tool: Any) -> Mapping[str, Any]:
    """Return protocol-alias MCP annotations for retry-safety decisions."""

    annotations = getattr(tool, "annotations", None)
    if annotations is None and isinstance(tool, Mapping):
        annotations = tool.get("annotations")
    return _mapping_value(annotations) or {}


def _explicit_tool_timeout_seconds(tool: Any, args: Mapping[str, Any]) -> float | None:
    """Read standard, explicitly supplied timeout arguments declared by the tool schema.

    Only arguments present in both the MCP input schema and this invocation count.
    Schema defaults are deliberately ignored, preserving the executor's existing
    default for callers that did not explicitly request a longer operation. An
    active ``wait_timeout_seconds`` participates only when ``wait_for_terminal`` is
    true; otherwise it does not describe this call's wall-clock lifetime.

    Explicit budgets are additive rather than alternatives. A tool may spend one
    budget executing remote work and another waiting or collecting its result;
    summing them is the only generic derivation that cannot undercut a valid
    sequential implementation. The executor's base timeout is added separately as
    transport/protocol overhead around those declared phases.
    """

    properties = _mapping_value(_tool_input_schema(tool).get("properties")) or {}
    candidates: list[float] = []
    for field in ("timeout_seconds", "wait_timeout_seconds"):
        if field not in properties or field not in args:
            continue
        if field == "wait_timeout_seconds" and args.get("wait_for_terminal") is not True:
            continue
        raw = args[field]
        if isinstance(raw, bool):
            continue
        try:
            seconds = float(raw)
        except (TypeError, ValueError):
            continue
        if math.isfinite(seconds) and seconds > 0:
            candidates.append(seconds)
    return sum(candidates) if candidates else None


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


class AsyncMCPToolExecutor:
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
    ) -> None:
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        cleaned_tool_timeouts = _clean_tool_timeouts(tool_timeouts)

        self._server = server
        # #932: namespace -> mounted proxy. A namespaced call routes straight
        # at ONE proxy (lazy client per namespace), so only the called
        # namespace backend ever spawns. The composite client remains the
        # fallback for names outside the map.
        self._namespace_servers = dict(namespace_servers) if namespace_servers else {}
        self._namespace_clients: dict[str, Any] = {}
        self._namespace_ctxs: dict[str, Any] = {}
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
        self._call_lock: asyncio.Lock | None = None
        self._started = False
        self._closed = False

    @property
    def started(self) -> bool:
        """Return whether the executor has discovered tools."""
        return self._started

    @property
    def closed(self) -> bool:
        """Return whether the executor has been closed."""
        return self._closed

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
        client = await client_ctx.__aenter__()
        if self._preloaded_tools is not None:
            # #932: tool definitions were preloaded (the boot listing pass) —
            # skip the list_tools fan-out that would eagerly spawn EVERY
            # mounted stdio server. Backends connect lazily per namespace on
            # the first call routed to them; a failed lazy connect surfaces as
            # that call's typed error, never a silent missing tool.
            self._client_ctx = client_ctx
            self._client = client
            self._mcp_tools = dict(self._preloaded_tools)
            self._call_lock = asyncio.Lock()
            self._started = True
            return self
        try:
            tools = await client.list_tools()
        except BaseException:
            with suppress(Exception):
                await client_ctx.__aexit__(None, None, None)
            raise

        self._client_ctx = client_ctx
        self._client = client
        self._mcp_tools = {tool.name: tool for tool in tools}
        self._call_lock = asyncio.Lock()
        self._started = True
        return self

    async def call_tool(self, name: str, args: Mapping[str, Any]) -> str:
        """Call an MCP tool on the caller's event loop."""

        outcome = await self.call_tool_result(name, args)
        return outcome.model_text

    async def call_tool_result(self, name: str, args: Mapping[str, Any]) -> _MCPCallOutcome:
        """Call an MCP tool and preserve its private raw result projection."""
        if self._closed:
            raise RuntimeError("AsyncMCPToolExecutor is closed")
        if self._client is None or self._call_lock is None:
            raise RuntimeError("AsyncMCPToolExecutor is not started")

        async with self._call_lock:
            prior_uncertain = self._prior_uncertain_mutating_timeout(name, args)
            if prior_uncertain is not None:
                raise UncertainMutatingToolOutcomeError(
                    name,
                    prior_uncertain,
                    retry_blocked=True,
                )
            budget = self._timeout_budget_for_call(name, args)
            timeout = budget.seconds
            client, on_server_name, namespace = await self._route(name)
            # #934: the namespace backend SPAWNS on its first forwarded call
            # (proxy ctx-enter spawns nothing), so first-call success/failure
            # is the spawn-diet learn/drop-plan signal.
            first_call = namespace is not None and namespace not in self._connected_namespaces
            try:
                result = await asyncio.wait_for(
                    client.call_tool(on_server_name, dict(args)),
                    timeout=timeout,
                )
            except TimeoutError as exc:
                # Conservative: a first-call timeout may be tool latency, not
                # spawn health — the dropped plan self-heals (declared respawn
                # + relearn). Distinguishing connect-failure from post-connect
                # errors needs an initialize-level signal (future refinement).
                if first_call and namespace is not None:
                    spawn_diet.spawn_failed(namespace)
                if not self._tool_timeout_is_retry_safe(name):
                    raise self.mark_uncertain_mutating_timeout(name, args, timeout) from exc
                raise TimeoutError(f"MCP tool {name!r} timed out after {timeout:g}s") from exc
            except Exception:
                if first_call and namespace is not None:
                    spawn_diet.spawn_failed(namespace)
                raise
            if first_call and namespace is not None:
                self._connected_namespaces.add(namespace)
                spawn_diet.namespace_connected(namespace)
        return _MCPCallOutcome(
            model_text=_result_to_text(result),
            raw_result=result,
            source_namespace=namespace,
        )

    async def read_resource(self, namespace: str | None, uri: str) -> Any:
        """Read ``uri`` from exactly ``namespace`` (or the composite gateway).

        MCP App resources are capability-bound to the server that produced the
        originating tool result. The caller supplies that recorded namespace;
        this method never searches or fans out across mounted servers.
        """

        if self._closed:
            raise RuntimeError("AsyncMCPToolExecutor is closed")
        if self._client is None or self._call_lock is None:
            raise RuntimeError("AsyncMCPToolExecutor is not started")
        parsed_uri = urlsplit(uri)
        if not parsed_uri.scheme:
            raise ValueError("MCP resource URI must be absolute")

        async with self._call_lock:
            if namespace:
                proxy = self._namespace_servers.get(namespace)
                if proxy is None:
                    raise ValueError(f"unknown MCP namespace {namespace!r}")
                client = self._namespace_clients.get(namespace)
                if client is None:
                    ctx = self._client_factory(proxy)
                    client = await ctx.__aenter__()
                    self._namespace_ctxs[namespace] = ctx
                    self._namespace_clients[namespace] = client
            else:
                client = self._client
            return await asyncio.wait_for(client.read_resource(uri), timeout=self._timeout)

    async def _route(self, name: str) -> tuple[Any, str, str | None]:
        """Resolve (client, on-server tool name, namespace|None) for a call.

        Namespaced tools route straight at their mounted proxy (#932): the
        composite gateway resolves names by listing EVERY mount, spawning the
        whole fleet; direct routing spawns only the called namespace backend.
        A failed proxy connect raises out of the CALL (the executor error
        path types it), never a silent missing tool. The namespace element is
        ``None`` for composite-routed names (the #934 first-call hooks only
        apply to namespace-direct backends).
        """

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
        client = self._namespace_clients.get(namespace)
        if client is None:
            ctx = self._client_factory(proxy)
            client = await ctx.__aenter__()
            self._namespace_ctxs[namespace] = ctx
            self._namespace_clients[namespace] = client
        return client, bare, namespace

    def _timeout_for_tool(self, name: str) -> float:
        """Return the effective timeout for a single tool invocation."""

        return self._tool_timeouts.get(name, self._timeout)

    def _timeout_budget_for_call(
        self,
        name: str,
        args: Mapping[str, Any],
    ) -> _ToolTimeoutBudget:
        """Return a timeout that cannot expire before an explicit tool-call budget."""

        base = self._timeout_for_tool(name)
        configured_explicitly = name in self._tool_timeouts
        declared = _explicit_tool_timeout_seconds(self._mcp_tools.get(name), args)
        if declared is None:
            return _ToolTimeoutBudget(base, explicitly_declared=configured_explicitly)
        return _ToolTimeoutBudget(base + declared, explicitly_declared=True)

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

        return [name for name, tool in self._mcp_tools.items() if _tool_visible_to_model(tool)]

    def get_tool_definitions(self) -> dict[str, Any]:
        """Return model-visible MCP tool definitions keyed by stable name."""

        return {
            name: tool for name, tool in self._mcp_tools.items() if _tool_visible_to_model(tool)
        }

    def get_all_tool_definitions(self) -> dict[str, Any]:
        """Return all definitions, including capability-bound app-only tools."""

        return dict(self._mcp_tools)

    async def aclose(self) -> None:
        """Close the client connection."""
        if self._closed:
            return
        self._closed = True

        for namespace, ctx in list(self._namespace_ctxs.items()):
            try:
                await asyncio.wait_for(ctx.__aexit__(None, None, None), timeout=5.0)
            except Exception as exc:  # noqa: BLE001 - teardown continues; reason logged
                logger.debug("Error closing namespace client %r: %s", namespace, exc)
        self._namespace_ctxs.clear()
        self._namespace_clients.clear()

        if self._client_ctx is not None:
            close_timeout = min(5.0, max(0.1, self._timeout))
            try:
                await asyncio.wait_for(
                    self._client_ctx.__aexit__(None, None, None),
                    timeout=close_timeout,
                )
            except Exception as exc:  # noqa: BLE001 - client-close error logged at debug; teardown continues
                logger.debug("Error closing AsyncMCPToolExecutor client: %s", exc)

        self._client = None
        self._client_ctx = None
        self._call_lock = None


def _result_to_text(result: Any) -> str:
    """Convert a FastMCP call result to the legacy string result shape."""
    data = getattr(result, "data", result)
    if isinstance(data, dict):
        return json.dumps(data)
    return str(data)


def _tool_ui_metadata(tool: Any) -> Mapping[str, Any]:
    """Return normalized MCP Apps metadata from a FastMCP tool definition."""

    if tool is None:
        return {}
    meta = getattr(tool, "meta", None) or getattr(tool, "_meta", None)
    if meta is None and isinstance(tool, Mapping):
        meta = tool.get("_meta") or tool.get("meta")
    meta_dump = getattr(meta, "model_dump", None)
    if callable(meta_dump):
        meta = meta_dump(by_alias=True, exclude_none=True)
    if not isinstance(meta, Mapping):
        return {}
    ui = meta.get("ui")
    ui_dump = getattr(ui, "model_dump", None)
    if callable(ui_dump):
        ui = ui_dump(by_alias=True, exclude_none=True)
    if isinstance(ui, Mapping):
        return ui
    # Deprecated flat metadata remains readable for interoperability, while
    # new servers should emit the stable nested ``_meta.ui`` shape.
    flat_uri = meta.get("ui/resourceUri")
    return {"resourceUri": flat_uri} if isinstance(flat_uri, str) else {}


def _tool_visible_to_model(tool: Any) -> bool:
    """Return whether a tool belongs on the model-facing tool surface."""

    visibility = _tool_ui_metadata(tool).get("visibility")
    if not isinstance(visibility, Sequence) or isinstance(visibility, (str, bytes)):
        return True
    return "model" in {str(item) for item in visibility}


__all__ = [
    "AsyncMCPToolExecutor",
    "ClientFactory",
    "MCPClientProtocol",
    "UncertainMutatingToolOutcomeError",
]
