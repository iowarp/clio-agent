"""Async MCP tool execution boundary.

This module owns the event-loop-native FastMCP executor, its narrow client
protocol, and the timeout and result-projection helpers required by that
executor.
"""

from __future__ import annotations

import asyncio
import base64
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
from clio_agent.tools.mcp_connection_era import (
    MCPConnectionEra,
    classify_connection_era,
    resolved_connect_mode,
)
from clio_agent.tools.mcp_errors import typed_mcp_call_error, typed_mcp_protocol_error
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


#: Backwards-compatible alias: the protocol-refusal mapping now lives in the shared
#: :mod:`clio_agent.tools.mcp_errors` seam every direct call path applies (#1114).
_typed_mcp_protocol_error = typed_mcp_protocol_error


@dataclass(frozen=True)
class _ToolTimeoutBudget:
    """One call's timeout (``None`` == unbounded commitment, #1225) + provenance."""

    seconds: float | None
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

    # fastmcp-4 / mcp-2 renamed ``Tool.inputSchema`` -> ``Tool.input_schema``. Read
    # the snake name FIRST so a live Tool never touches its deprecated camelCase
    # alias (which warns, and raises when FastMCP's compat shim is disabled). The
    # camelCase name is accepted ONLY as a fallback for objects/mappings that lack
    # the snake name (persisted/wire rows, legacy fixtures).
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
        server_id: str = "",
    ) -> None:
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        cleaned_tool_timeouts = _clean_tool_timeouts(tool_timeouts)

        self._server = server
        # #1201: identity label for the primary connection's era record below.
        self._server_id = server_id
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
            client = await client_ctx.__aenter__()
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
                raise UncertainMutatingToolOutcomeError(name, prior_uncertain, retry_blocked=True)
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
                assert timeout is not None, "unbounded wait never times out"
                if not self._tool_timeout_is_retry_safe(name):
                    raise self.mark_uncertain_mutating_timeout(name, args, timeout) from exc
                raise TimeoutError(f"MCP tool {name!r} timed out after {timeout:g}s") from exc
            except Exception as exc:
                if first_call and namespace is not None:
                    spawn_diet.spawn_failed(namespace)
                # #1114: the ONE shared boundary translation (MRTR exhaustion +
                # protocol refusals) every direct call path applies.
                typed_error = typed_mcp_call_error(exc, tool=name)
                if typed_error is not None:
                    raise typed_error from exc
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
                    client = await self._connect_namespace(namespace, proxy)
            else:
                client = self._client
            return await asyncio.wait_for(client.read_resource(uri), timeout=self._timeout)

    async def _connect_namespace(self, namespace: str, proxy: Any) -> Any:
        """Connect + cache a namespace-direct client, stamping its era (#1201)."""

        ctx = self._client_factory(proxy)
        client = await ctx.__aenter__()
        self._namespace_ctxs[namespace] = ctx
        self._namespace_clients[namespace] = client
        self._namespace_connection_eras[namespace] = classify_connection_era(
            server_id=namespace,
            protocol_version=getattr(client, "protocol_version", None),
            connect_mode=resolved_connect_mode(),
        )
        return client

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
            client = await self._connect_namespace(namespace, proxy)
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


#: Finding E (proven degradation, tools/mcp_executor.py review): the
#: last-resort ``str(data)`` fallback below reintroduces Python repr text on
#: the wire -- the exact defect ``_result_to_text`` exists to close for the
#: JSON-encodable case. It stays, because a handful of values genuinely have
#: no JSON mapping, but the degradation is never silent: this typed reason
#: reaches the log the same way every other degradation in this package does
#: (``execution.py``'s ``reason=tool_observer_failed`` /
#: ``reason=file_policy_unavailable`` idiom; ``gateway.py``'s
#: ``reason=%s`` degrade logs).
MCP_RESULT_TO_TEXT_REPR_FALLBACK_REASON = "mcp_result_to_text_repr_fallback"


def _content_block_field(block: Any, *names: str) -> Any:
    """Read the first present field from a content block (mapping or SDK model)."""

    if isinstance(block, Mapping):
        for field_name in names:
            value = block.get(field_name)
            if value is not None:
                return value
        return None
    for field_name in names:
        value = getattr(block, field_name, None)
        if value is not None:
            return value
    return None


def _base64_decoded_length(data: str) -> int:
    """Approximate decoded byte length of a base64 string without decoding it."""

    if not data:
        return 0
    padding = len(data) - len(data.rstrip("="))
    return max(0, (len(data) * 3) // 4 - padding)


def _human_bytes(num_bytes: int) -> str:
    """Compact human-readable byte size for a model-facing placeholder."""

    if num_bytes < 1024:
        return f"{num_bytes}B"
    if num_bytes < 1024 * 1024:
        return f"{num_bytes / 1024:.1f}KB"
    return f"{num_bytes / (1024 * 1024):.1f}MB"


_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_GIF_SIGNATURES = (b"GIF87a", b"GIF89a")


def _raster_dimensions(mime_type: str, data_b64: str) -> tuple[int, int] | None:
    """Best-effort ``(width, height)`` for a PNG/GIF block from a bounded prefix.

    Decodes only the leading ~48 raw bytes (never the full image) -- just
    enough to cover the PNG IHDR chunk or the GIF logical screen descriptor.
    Any other format, or a prefix too short/garbled to parse, returns ``None``
    (never guessed); the caller falls back to a byte-size placeholder.
    """

    prefix = data_b64[:80]
    prefix += "=" * (-len(prefix) % 4)
    try:
        raw = base64.b64decode(prefix, validate=False)
    except ValueError:
        return None
    mime = (mime_type or "").lower()
    if mime == "image/png" and raw[:8] == _PNG_SIGNATURE and len(raw) >= 24:
        return int.from_bytes(raw[16:20], "big"), int.from_bytes(raw[20:24], "big")
    if mime == "image/gif" and raw[:6] in _GIF_SIGNATURES and len(raw) >= 10:
        return int.from_bytes(raw[6:8], "little"), int.from_bytes(raw[8:10], "little")
    return None


def _content_block_model_text(block: Any) -> str:
    """Compact model-facing placeholder/text for one MCP content block.

    A ``text`` block contributes its text VERBATIM (the common unstructured-
    content case: the tool's whole result IS its text). Every other block
    type contributes a short bracket placeholder -- NEVER the block's raw
    payload (base64 image/audio data, an embedded resource blob). The model
    gets enough to know the evidence exists and reason about it; the bytes
    themselves reach the wire only through the tool_result Part's own
    ``content_blocks`` field (``gact/tool_observer.py``), never through this
    model-facing lane.
    """

    block_type = str(_content_block_field(block, "type") or "")
    if block_type == "text":
        return str(_content_block_field(block, "text") or "")
    mime_type = str(_content_block_field(block, "mime_type", "mimeType") or "")
    if block_type == "image":
        data = str(_content_block_field(block, "data") or "")
        dims = _raster_dimensions(mime_type, data)
        detail = f"{dims[0]}x{dims[1]}" if dims else _human_bytes(_base64_decoded_length(data))
        return f"[image {mime_type or 'unknown'} {detail}]"
    if block_type == "audio":
        data = str(_content_block_field(block, "data") or "")
        return f"[audio {mime_type or 'unknown'} {_human_bytes(_base64_decoded_length(data))}]"
    if block_type == "resource":
        resource = _content_block_field(block, "resource")
        uri = str(_content_block_field(resource, "uri") or "") if resource is not None else ""
        resource_mime = (
            str(_content_block_field(resource, "mime_type", "mimeType") or "")
            if resource is not None
            else ""
        )
        return f"[resource {resource_mime or mime_type or 'unknown'} {uri}]".rstrip()
    if block_type == "resource_link":
        uri = str(_content_block_field(block, "uri") or "")
        name = str(_content_block_field(block, "name") or "")
        return f"[resource_link {name or uri}]"
    return f"[{block_type}]" if block_type else ""


def _result_to_text(result: Any) -> str:
    """Convert a FastMCP call result to the legacy string model-facing text.

    ``data`` is the client's structured projection of the tool result: FastMCP
    wraps a non-object return (list, str, bool, ...) in ``{"result": ...}`` on
    the wire, and the client unwraps it back to the tool's native Python type
    (``fastmcp.client.mixins.tools._parse_call_tool_result``). Only ``dict``
    was JSON-encoded here; every other structured shape fell through to
    Python's ``str()``, which renders single-quoted repr syntax for a list of
    dicts (valid only via ``ast.literal_eval``, not JSON) and capitalized
    ``True``/``False`` for booleans -- observed live as MCP fleet results
    (e.g. ``geo_geocode``) rendering as Python repr text on the wire instead
    of structured JSON that the UI's result ladder can render. A bare string
    ``data`` is returned verbatim (it is already model-facing text, not a
    value to re-encode); every other shape is JSON-encoded, falling back to
    ``str()`` only for genuinely unserializable values.

    ``data`` is derived ONLY from ``structuredContent`` on the client side
    (``_parse_call_tool_result``) -- a tool that returns PURE content blocks
    with no structured output (``fastmcp.Image``/``Audio``, or a bare list of
    ``TextContent`` blocks) parses to ``data=None``. Before this fix that flowed
    straight into ``json.dumps(None)`` and the model observed the literal
    string ``"null"`` for a result that plainly carried evidence. When ``data``
    is ``None`` and ``result.content`` is non-empty, the text is now built from
    the content blocks themselves via :func:`_content_block_model_text` instead
    (TEXT blocks verbatim, everything else a short placeholder -- never raw
    base64 in the model-facing lane).

    ``allow_nan=False`` keeps NaN/Infinity out of the encoded text: Python's
    ``json`` module happily emits the non-standard tokens ``NaN`` / ``Infinity``
    by default, which is invalid JSON everywhere else, so they are routed to
    the same typed fallback as any other unencodable value instead of landing
    on the wire as JSON that isn't actually valid JSON. The fallback itself
    catches every structural reason ``json.dumps`` can refuse a value --
    ``TypeError`` (no JSON mapping), ``ValueError`` (circular reference, or
    NaN/Infinity under ``allow_nan=False``), ``RecursionError`` (a
    pathologically self-referential structure exhausting the recursion limit
    before json's own cycle guard fires), ``OverflowError`` (an int outside
    the encoder's range) -- and logs a structured, typed reason
    (:data:`MCP_RESULT_TO_TEXT_REPR_FALLBACK_REASON`) before returning the
    repr text, so the degradation reaches the log/trace channel instead of
    silently reintroducing repr-on-the-wire.
    """
    data = getattr(result, "data", result)
    if isinstance(data, str):
        return data
    if data is None:
        content = getattr(result, "content", None)
        if (
            isinstance(content, Sequence)
            and not isinstance(content, (str, bytes, bytearray))
            and content
        ):
            placeholder = "\n".join(
                piece for piece in (_content_block_model_text(block) for block in content) if piece
            )
            if placeholder:
                return placeholder
    try:
        return json.dumps(data, allow_nan=False)
    except (TypeError, ValueError, RecursionError, OverflowError) as exc:
        logger.warning(
            "mcp result to text degraded to repr fallback reason=%s type=%s error=%s",
            MCP_RESULT_TO_TEXT_REPR_FALLBACK_REASON,
            type(data).__name__,
            exc,
        )
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
