"""Tool execution boundaries for CLIO experts."""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextvars
import hashlib
import inspect
import json
import logging
import math
import os
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Optional, Protocol, cast
from urllib.parse import urlsplit

import dspy
from fastmcp import Client

from clio_agent import conf
from clio_agent.errors import CancellationError
from clio_agent.runtime.stream_audit import stream_audit
from clio_agent.tools import spawn_diet
from clio_agent.tools.file_policy import FileAccessPolicy
from clio_agent.tools.mcp_results import call_tool_result_to_observer

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


# iowarp/clio-agent#7 + #2 + #735: the four tool-runtime hooks (permission
# gate, telemetry observer, preflight interceptor, cancellation checker) are
# resolved per tool call through the ``ToolRuntimeHooks`` seam below — gact
# installs a stateless resolver that dispatches on the LIVE turn's app, so
# concurrent apps in one process never share a hook. There is no process-global
# hook state left; the sole retained net is the single ``_FALLBACK_TOOL_RUNTIME``
# bundle consulted (loudly) only when no app resolves.
ToolObserver = Callable[
    [str, Mapping[str, Any], Optional[str], Optional[str], Any | None],
    None,
]
LegacyToolObserver = Callable[[str, Mapping[str, Any], Optional[str], Optional[str]], None]
MCPAppObserver = Callable[[str, Mapping[str, Any], Any, Any, str | None], None]
PermissionGate = (
    Callable[[str, Mapping[str, Any]], str]
    | Callable[[str, Mapping[str, Any], Mapping[str, Any]], str]
)

# The active session workspace root rides its own ContextVar (kept: it is read on
# the app-less CLI grounding path where no app resolves — see ``agent.py`` /
# ``file_policy.py`` — so it cannot fold into the ``active_app()``-keyed bundle).
_ACTIVE_TOOL_WORKSPACE_ROOT: contextvars.ContextVar[str] = contextvars.ContextVar(
    "clio_active_tool_workspace_root",
    default="",
)


@contextmanager
def tool_workspace_context(root: str | Path | None) -> Iterator[None]:
    """Bind the active session workspace root for default tool artifacts."""

    token = _ACTIVE_TOOL_WORKSPACE_ROOT.set(str(root or ""))
    try:
        yield
    finally:
        _ACTIVE_TOOL_WORKSPACE_ROOT.reset(token)


def get_active_tool_workspace_root() -> str:
    """Return the active session workspace root, or ``""`` when none is bound."""

    return _ACTIVE_TOOL_WORKSPACE_ROOT.get()


# --------------------------------------------------------------------------- #
# iowarp/clio-agent#735 — the tool-runtime hooks SEAM (unified concurrency §2). #
#                                                                               #
# The low ``tools`` layer owns an inversion-of-control SLOT: a frozen data      #
# shape (``ToolRuntimeHooks``), one resolver function pointer, and one retained #
# fallback bundle. gact installs a STATELESS resolver once (``build_app`` ->     #
# ``set_tool_runtime_resolver``) that dispatches on the live turn's app; the    #
# executor reads the bundle via ``current_tool_runtime()`` at call time. Nothing #
# per-app is ever pushed into this layer, and this layer imports no ``gact``.    #
#                                                                               #
# This is the SOLE tool-hook mechanism: the resolver is the in-turn path and the #
# single ``_FALLBACK_TOOL_RUNTIME`` bundle is the reason-logged app-less net.    #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ToolRuntimeHooks:
    """The per-tool-call hook bundle resolved for the live turn (#735).

    A frozen value object carrying the tool-runtime hooks. ``None`` on any
    field means "no such hook" (a no-op), never "look elsewhere".
    """

    permission_gate: PermissionGate | None = None
    tool_observer: Optional[ToolObserver | LegacyToolObserver] = None
    tool_interceptor: Optional[Callable[[str, Mapping[str, Any]], Any | None]] = None
    cancellation_checker: Optional[Callable[[], bool]] = None
    # The ordinary observer receives only the sanitized public MCP projection.
    # MCP Apps additionally need the full CallToolResult (including private
    # ``_meta``), which stays in a capability-bound, session-local store.
    mcp_app_observer: Optional[MCPAppObserver] = None


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


# One installed resolver slot (gact fills it once) + one retained fallback bundle
# used ONLY when no app resolves (out-of-band / app-less caller).
_TOOL_RUNTIME_RESOLVER: Optional[Callable[[], "ToolRuntimeHooks | None"]] = None
_FALLBACK_TOOL_RUNTIME: ToolRuntimeHooks = ToolRuntimeHooks()


def set_tool_runtime_resolver(fn: Optional[Callable[[], "ToolRuntimeHooks | None"]]) -> None:
    """Install the stateless per-call resolver (gact does this once in build_app).

    The resolver returns the live turn's hooks, or ``None`` when app-less so
    ``current_tool_runtime`` takes the reason-logged fallback path.
    """

    global _TOOL_RUNTIME_RESOLVER
    _TOOL_RUNTIME_RESOLVER = fn


def set_tool_runtime_fallback(hooks: ToolRuntimeHooks) -> None:
    """Set the neutral app-less fallback bundle (defaults to empty hooks).

    Production leaves this NEUTRAL: gact's per-app install stamps ``app.state``
    only and the resolver dispatches on ``active_app()``, so an app-less resolve
    never returns a sibling app's live hooks (#735 unified §1). This setter exists
    for explicit out-of-band / test callers that deliberately exercise the app-less
    path; such callers own resetting it back to ``ToolRuntimeHooks()``.
    """

    global _FALLBACK_TOOL_RUNTIME
    _FALLBACK_TOOL_RUNTIME = hooks


# Structured reason catalog for a degraded tool-runtime resolve — modeled on the
# ``stream_fallback`` catalog (a typed reason, recorded + queryable after the
# fact). Every app-less resolve emits one of these instead of silently returning
# an empty bundle (the no-silent-fallback ground rule): a dropped permission gate
# must always be observable in the audit sink.
_TOOL_RUNTIME_REASON_DEFINITIONS: dict[str, dict[str, Any]] = {
    "tool_runtime_appless_fallback": {
        "severity": "warning",
        "detail": (
            "no app resolved for this tool call; used the retained last-installed "
            "hook bundle (out-of-band / app-less caller)"
        ),
    },
    "tool_runtime_unresolved": {
        "severity": "warning",
        "detail": (
            "no app resolved and the retained fallback bundle carries no permission "
            "gate or observer; the tool call runs unhooked"
        ),
    },
}

_TOOL_RUNTIME_REASONS: "deque[dict[str, Any]]" = deque(maxlen=256)
_TOOL_RUNTIME_REASONS_LOCK = threading.Lock()


def _emit_tool_runtime_reason(reason: str, **fields: Any) -> dict[str, Any]:
    """Record a typed tool-runtime resolve reason to the low-layer audit sink.

    Appends to the bounded, queryable in-process ring (``recorded_tool_runtime_reasons``)
    and to the ``stream_audit`` JSONL. Does NOT import gact.
    """

    definition = _TOOL_RUNTIME_REASON_DEFINITIONS.get(reason)
    if definition is None:
        raise ValueError(f"Unknown tool-runtime reason: {reason}")
    payload: dict[str, Any] = {"reason": reason, **definition, **fields}
    with _TOOL_RUNTIME_REASONS_LOCK:
        _TOOL_RUNTIME_REASONS.append(payload)
    stream_audit("tool_runtime_fallback", **payload)
    logger.debug(
        "tool-runtime resolve degraded reason=%s detail=%s",
        reason,
        definition["detail"],
    )
    return payload


def recorded_tool_runtime_reasons() -> list[dict[str, Any]]:
    """Return a snapshot of recorded tool-runtime resolve reasons (queryable audit)."""

    with _TOOL_RUNTIME_REASONS_LOCK:
        return list(_TOOL_RUNTIME_REASONS)


def current_tool_runtime() -> ToolRuntimeHooks:
    """Resolve the live tool-runtime hook bundle for THIS tool call (#735 seam).

    Prefers the installed resolver (gact dispatches on ``active_app()`` so N apps
    in one process each read their own ``app.state.pending_*``). When no app
    resolves it falls back to the single retained ``_FALLBACK_TOOL_RUNTIME``
    bundle — but LOUDLY: it emits a structured reason so the degradation reaches
    the audit sink rather than silently dropping a gate.
    """

    resolver = _TOOL_RUNTIME_RESOLVER
    resolved = resolver() if resolver is not None else None
    if resolved is not None:
        return resolved
    fallback = _FALLBACK_TOOL_RUNTIME
    _emit_tool_runtime_reason(
        "tool_runtime_appless_fallback"
        if fallback.permission_gate is not None or fallback.tool_observer is not None
        else "tool_runtime_unresolved"
    )
    return fallback


def notify_tool_observer(
    observer: Optional[ToolObserver | LegacyToolObserver],
    name: str,
    args: Mapping[str, Any],
    phase: str,
    error: str | None = None,
    result: Any | None = None,
) -> None:
    """Notify a tool observer, swallowing observer failures."""

    if observer is None:
        return
    try:
        if result is None:
            observer(name, dict(args), phase, error)  # type: ignore[misc, call-arg]
        else:
            try:
                observer(name, dict(args), phase, error, result)  # type: ignore[misc, call-arg]
            except TypeError:
                observer(name, dict(args), phase, error)  # type: ignore[misc, call-arg]
    except Exception as exc:  # noqa: BLE001 - observers must never break tool execution
        logger.warning(
            "tool observer raised; its view of this call is lost "
            "reason=tool_observer_failed tool=%s phase=%s error=%s",
            name,
            phase,
            exc,
        )


def notify_global_tool_observer(
    name: str,
    args: Mapping[str, Any],
    phase: str,
    error: str | None = None,
    result: Any | None = None,
) -> None:
    """Notify the active tool observer (per-turn override, else global fallback).

    Prefers the current turn's observer so an in-turn caller (a live-observed
    agent, a native tool shim) reaches THIS app's observer rather than a sibling
    app's process-global (iowarp/clio-agent#735).
    """

    notify_tool_observer(current_tool_runtime().tool_observer, name, args, phase, error, result)


def _structured_tool_result_error(result: Any) -> str | None:
    """Return an error string when a tool returns a structured error payload."""

    decoded = result
    if isinstance(result, str):
        stripped = result.strip()
        if stripped.startswith("{") and '"error"' in stripped:
            with suppress(json.JSONDecodeError, TypeError):
                decoded = json.loads(stripped)
    if isinstance(decoded, Mapping):
        error = decoded.get("error")
        if error:
            if isinstance(error, Mapping):
                code = str(error.get("code") or error.get("type") or "tool_error")
                message = str(error.get("message") or "").strip()
                return f"{code}: {message}" if message else code
            return str(error)
        status = str(decoded.get("status") or "").strip().lower()
        if status in {"error", "failed", "failure"}:
            message = str(decoded.get("message") or decoded.get("detail") or "").strip()
            return f"status={status}: {message}" if message else f"status={status}"
        if decoded.get("ok") is False:
            message = str(decoded.get("message") or decoded.get("detail") or "").strip()
            return f"ok=false: {message}" if message else "ok=false"
    elif isinstance(decoded, str):
        normalized = decoded.strip().casefold()
        if normalized.startswith("error:"):
            return decoded.strip()
    return None


class AsyncToolExecutor(Protocol):
    """Native async tool execution interface for API/service callers."""

    async def start(self) -> "AsyncToolExecutor":
        """Initialize backing tool resources and discover tools."""
        ...

    async def call_tool(self, name: str, args: Mapping[str, Any]) -> str:
        """Call a named tool asynchronously and return a string result."""
        ...

    def get_tool_names(self) -> list[str]:
        """Return all discovered tool names."""
        ...

    async def aclose(self) -> None:
        """Release async tool resources."""
        ...


class SyncToolExecutor(Protocol):
    """Synchronous tool execution interface used by CLI and native expert callers."""

    def call_tool(self, name: str, args: Mapping[str, Any]) -> str:
        """Call a named tool and return a string result."""
        ...

    def get_tool_names(self) -> list[str]:
        """Return all tool names exposed by this executor."""
        ...

    def to_dspy_tools(self) -> list[dspy.Tool]:
        """Convert executor-backed tools to DSPy tool objects."""
        ...

    def close(self) -> None:
        """Release tool resources."""
        ...


ToolExecutor = SyncToolExecutor

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


class RepeatedToolFailureError(RuntimeError):
    """Raised when a tool keeps failing with transient infrastructure errors."""


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


def _invoke_permission_gate(
    gate: PermissionGate,
    name: str,
    args: Mapping[str, Any],
    context: Mapping[str, Any] | None,
) -> str:
    """Invoke a permission gate with MCP context when its signature accepts it.

    Two-argument permission hooks are a public compatibility seam. Signature
    binding selects the legacy form before invocation instead of catching a
    ``TypeError`` from inside the hook and accidentally executing it twice.
    """

    if context is None:
        return gate(name, args)  # type: ignore[call-arg]
    try:
        gate_signature = inspect.signature(gate)
    except (TypeError, ValueError):
        return gate(name, args, context)  # type: ignore[call-arg]
    try:
        gate_signature.bind(name, args, context)
    except TypeError:
        return gate(name, args)  # type: ignore[call-arg]
    return gate(name, args, context)  # type: ignore[call-arg]


def _declared_mcp_permission_context(
    name: str,
    tool: Any,
    namespace_servers: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Return annotation context for a tool owned by a declared MCP server.

    Built-in ``fs``/``shell`` tools retain their existing explicit semantics.
    Every configured server in ``namespace_servers`` is an external MCP
    capability, so missing or malformed annotations remain visible to the GACT
    gate and fail closed there.
    """

    namespace, separator, _bare_name = name.partition("_")
    if not separator or namespace not in namespace_servers:
        return None
    return {
        "kind": "external_mcp",
        "annotations": dict(_tool_annotations(tool)),
    }


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


def _is_transient_tool_error(error_text: str) -> bool:
    """Return whether an error indicates infrastructure/service instability."""

    lowered = error_text.lower()
    transient_terms = (
        "closedresourceerror",
        "connectionreseterror",
        "connectionerror",
        "connecterror",
        "readtimeout",
        "timeout",
        "timed out",
        "temporarily unavailable",
        "service unavailable",
        "server disconnected",
        "brokenpipeerror",
    )
    return any(term in lowered for term in transient_terms)


def create_async_tool_executor(
    server: Any,
    *,
    timeout: float = 30.0,
    tool_timeouts: Mapping[str, float] | None = None,
    client_factory: ClientFactory | None = None,
    preloaded_tools: Mapping[str, Any] | None = None,
    namespace_servers: Mapping[str, Any] | None = None,
) -> "AsyncMCPToolExecutor":
    """Create an async FastMCP-backed tool executor.

    The caller owns startup and shutdown:

    - `await executor.start()` or `async with executor`
    - `await executor.aclose()`
    """
    return AsyncMCPToolExecutor(
        server,
        timeout=timeout,
        tool_timeouts=tool_timeouts,
        client_factory=client_factory,
        preloaded_tools=preloaded_tools,
        namespace_servers=namespace_servers,
    )


def create_sync_tool_executor(
    server: Any,
    *,
    timeout: float = 30.0,
    setup_timeout: float | None = None,
    tool_timeouts: Mapping[str, float] | None = None,
    client_factory: ClientFactory | None = None,
    preloaded_tools: Mapping[str, Any] | None = None,
    namespace_servers: Mapping[str, Any] | None = None,
) -> SyncToolExecutor:
    """Create a sync executor for CLI and deterministic expert call sites."""
    effective_setup_timeout = (
        conf.resolve(
            "tools.mcp.setup_timeout_s",
            env="CLIO_MCP_SETUP_TIMEOUT_S",
            default=10.0,
            cast=conf.as_float,
        )
        if setup_timeout is None
        else setup_timeout
    )
    return SyncMCPToolExecutor(
        server,
        timeout=timeout,
        setup_timeout=effective_setup_timeout,
        tool_timeouts=tool_timeouts,
        client_factory=client_factory,
        preloaded_tools=preloaded_tools,
        namespace_servers=namespace_servers,
    )


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
        self._client_factory = cast(ClientFactory, client_factory or Client)
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


class SyncMCPToolExecutor:
    """Sync adapter for async MCP tools.

    This is the CLI/DSPy path. It owns one event-loop thread per executor and
    delegates all FastMCP work to `AsyncMCPToolExecutor`, making the sync/async
    boundary explicit and replaceable.
    """

    def __init__(
        self,
        server: Any,
        timeout: float = 30.0,
        setup_timeout: float = 10.0,
        tool_timeouts: Mapping[str, float] | None = None,
        client_factory: ClientFactory | None = None,
        permission_gate: PermissionGate | None = None,
        tool_observer: Optional[ToolObserver | LegacyToolObserver] = None,
        preloaded_tools: Mapping[str, Any] | None = None,
        namespace_servers: Mapping[str, Any] | None = None,
    ):
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        if setup_timeout <= 0:
            raise ValueError("setup_timeout must be positive")
        cleaned_tool_timeouts = _clean_tool_timeouts(tool_timeouts)

        self._timeout = timeout
        self._setup_timeout = setup_timeout
        self._tool_timeouts = cleaned_tool_timeouts
        self._async_executor = AsyncMCPToolExecutor(
            server,
            timeout=timeout,
            preloaded_tools=preloaded_tools,
            namespace_servers=namespace_servers,
            tool_timeouts=cleaned_tool_timeouts,
            client_factory=client_factory,
        )
        # iowarp/clio-agent#7: optional gate called BEFORE every
        # tool invocation. Returns one of:
        #   "allow"  → run the tool unchanged
        #   "deny"   → raise a PermissionError; the agent sees the
        #              traceback in its tool_result and reports it.
        # Explicit instance hook wins. When omitted, call_tool consults the
        # resolved ``current_tool_runtime()`` bundle dynamically so GACT deferred
        # startup can wire hooks after an executor exists.
        self._permission_gate = permission_gate
        # iowarp/clio-agent#2: optional observer called BEFORE
        # ("started") and AFTER ("completed", error?) every tool
        # invocation. Same global-fallback story.
        self._tool_observer = tool_observer
        self._failure_lock = threading.Lock()
        self._consecutive_transient_failures: dict[str, tuple[int, str]] = {}
        self._closed = False
        self._close_lock = threading.Lock()
        # Reaper instrumentation (#933): last-activity clock + in-flight count.
        self._last_activity = time.monotonic()
        self._inflight = 0
        self._inflight_lock = threading.Lock()

        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._run_loop,
            name="clio-sync-mcp-tool-executor",
            daemon=True,
        )
        self._thread.start()

        try:
            self._run_coroutine(
                self._async_executor.start(),
                timeout=setup_timeout,
                action="MCP executor setup",
            )
        except TimeoutError:
            self.close()
            raise
        except Exception as exc:
            self.close()
            raise RuntimeError(f"SyncMCPToolExecutor setup failed: {exc}") from exc

    @property
    def closed(self) -> bool:
        """Return whether the executor has been closed."""
        return self._closed

    @property
    def busy(self) -> bool:
        """Return whether a tool call is currently in flight (#933 drain guard)."""
        with self._inflight_lock:
            return self._inflight > 0

    def idle_for(self) -> float:
        """Seconds since the last call started or finished (#933 idle TTL clock)."""
        with self._inflight_lock:
            return time.monotonic() - self._last_activity

    @property
    def _mcp_tools(self) -> dict[str, Any]:
        """Compatibility access to discovered MCP tool definitions."""
        return self._async_executor._mcp_tools

    def __enter__(self) -> "SyncMCPToolExecutor":
        """Return this executor from a sync context manager."""
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        """Close this executor from a sync context manager."""
        self.close()

    def _run_loop(self) -> None:
        """Run the background event loop until close()."""
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_forever()
        finally:
            pending = [task for task in asyncio.all_tasks(self._loop) if not task.done()]
            for task in pending:
                task.cancel()
            if pending:
                self._loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            with suppress(Exception):
                self._loop.run_until_complete(self._loop.shutdown_asyncgens())
            self._loop.close()

    def call_tool(self, name: str, args: Mapping[str, Any]) -> str:
        """Call an MCP tool synchronously via the background event loop.

        Two optional injection points fire around the underlying
        FastMCP call:
          1. ``permission_gate(name, args) -> {"allow"|"deny"}`` —
             when configured, runs first. "deny" raises
             PermissionError; the ReAct loop sees the traceback in
             the tool_result and reports it back as the assistant
             answer.
          2. ``tool_observer(name, args, phase, error?, result?)`` —
             non-blocking notifications of "started" + "completed"
             so the GACT layer can publish tool.call.* events and bounded
             returned evidence.
        """

        if self._closed:
            raise RuntimeError("SyncMCPToolExecutor is closed")
        with self._inflight_lock:
            self._inflight += 1
            self._last_activity = time.monotonic()
        try:
            return self._call_tool_inner(name, args, return_raw=False)
        finally:
            with self._inflight_lock:
                self._inflight -= 1
                self._last_activity = time.monotonic()

    def call_tool_result(self, name: str, args: Mapping[str, Any]) -> Any:
        """Call a tool through the normal gate/observers and return CallToolResult.

        This is reserved for the MCP Apps bridge. Agent-facing callers must use
        :meth:`call_tool`, whose result is the legacy trace-safe text projection.
        """

        if self._closed:
            raise RuntimeError("SyncMCPToolExecutor is closed")
        with self._inflight_lock:
            self._inflight += 1
            self._last_activity = time.monotonic()
        try:
            return self._call_tool_inner(name, args, return_raw=True)
        finally:
            with self._inflight_lock:
                self._inflight -= 1
                self._last_activity = time.monotonic()

    def _call_tool_inner(
        self,
        name: str,
        args: Mapping[str, Any],
        *,
        return_raw: bool,
    ) -> Any:
        hooks = current_tool_runtime()
        permission_gate = self._permission_gate or hooks.permission_gate
        tool_observer = self._tool_observer or hooks.tool_observer
        mcp_app_observer = hooks.mcp_app_observer
        cancellation_checker = hooks.cancellation_checker

        def raise_if_cancelled(stage: str) -> None:
            if cancellation_checker is not None and cancellation_checker():
                raise CancellationError(
                    "tool call cancelled by client",
                    details={
                        "tool": name,
                        "execution_cancellation": "cooperative",
                        "executor_work_may_continue": False,
                        "stage": stage,
                    },
                )

        effective_args, repair_records = _repair_missing_file_arguments(args)
        effective_args = _ground_output_paths(
            effective_args,
            getattr(self._mcp_tools.get(name), "inputSchema", None),
            get_active_tool_workspace_root(),
        )

        if permission_gate is not None:
            permission_context = _declared_mcp_permission_context(
                name,
                self._mcp_tools.get(name),
                self._async_executor._namespace_servers,
            )
            try:
                decision = _invoke_permission_gate(
                    permission_gate,
                    name,
                    dict(effective_args),
                    permission_context,
                )
            except Exception as exc:  # noqa: BLE001
                raise PermissionError(f"permission gate raised: {exc!r}") from exc
            if decision != "allow":
                raise PermissionError(f"tool call {name!r} denied by permission gate")

        raise_if_cancelled("tool_call_before")

        circuit_error = self._repeated_transient_failure_error(name)
        if circuit_error is not None:
            notify_tool_observer(tool_observer, name, effective_args, "started", None)
            notify_tool_observer(tool_observer, name, effective_args, "completed", circuit_error)
            raise RepeatedToolFailureError(circuit_error)

        tool_interceptor = hooks.tool_interceptor
        if tool_interceptor is not None:
            intercepted = tool_interceptor(name, dict(effective_args))
            if intercepted is not None:
                notify_tool_observer(tool_observer, name, effective_args, "started", None)
                notify_tool_observer(
                    tool_observer, name, effective_args, "completed", None, intercepted
                )
                return intercepted

        notify_tool_observer(tool_observer, name, effective_args, "started", None)

        budget = self._async_executor._timeout_budget_for_call(name, effective_args)
        timeout = budget.seconds
        try:
            outcome = self._run_coroutine(
                self._async_executor.call_tool_result(name, effective_args),
                timeout=timeout + SYNC_TOOL_RESULT_GRACE_SECONDS,
                action=f"MCP tool {name!r}",
            )
            raise_if_cancelled("tool_call_after")
        except Exception as exc:
            if isinstance(
                exc, TimeoutError
            ) and not self._async_executor._tool_timeout_is_retry_safe(name):
                uncertain = self._async_executor.mark_uncertain_mutating_timeout(
                    name,
                    effective_args,
                    timeout,
                )
                error_text = repr(uncertain)
                notify_tool_observer(
                    tool_observer,
                    name,
                    effective_args,
                    "completed",
                    error_text,
                )
                raise uncertain from exc
            error_text = repr(exc)
            if not isinstance(exc, UncertainMutatingToolOutcomeError):
                self._record_tool_failure(name, error_text)
            notify_tool_observer(tool_observer, name, effective_args, "completed", error_text)
            raise
        result = outcome.model_text
        observer_result = call_tool_result_to_observer(outcome.raw_result)
        structured_error = _structured_tool_result_error(result)
        if structured_error:
            self._record_tool_failure(name, structured_error)
            notify_tool_observer(
                tool_observer,
                name,
                effective_args,
                "completed",
                structured_error,
                observer_result,
            )
        else:
            self._record_tool_success(name)
            notify_tool_observer(
                tool_observer,
                name,
                effective_args,
                "completed",
                None,
                observer_result,
            )
            if mcp_app_observer is not None:
                try:
                    mcp_app_observer(
                        name,
                        effective_args,
                        self._mcp_tools.get(name),
                        outcome.raw_result,
                        outcome.source_namespace,
                    )
                except Exception as exc:  # noqa: BLE001 - the tool itself succeeded
                    logger.exception(
                        "MCP App result admission failed tool=%r reason=mcp_app_admission_failed: %s",
                        name,
                        exc,
                    )

        if return_raw:
            return outcome.raw_result
        if repair_records:
            return _prepend_repair_notes(repair_records, result)
        return result

    def read_resource(self, namespace: str | None, uri: str) -> Any:
        """Read one resource from the exact originating MCP namespace."""

        if self._closed:
            raise RuntimeError("SyncMCPToolExecutor is closed")
        return self._run_coroutine(
            self._async_executor.read_resource(namespace, uri),
            timeout=self._timeout,
            action=f"MCP resource {uri!r}",
        )

    def _repeated_transient_failure_error(self, name: str) -> str | None:
        """Return a structured error when the tool circuit should stay open."""

        with self._failure_lock:
            count, last_error = self._consecutive_transient_failures.get(name, (0, ""))
        if count < REPEATED_TRANSIENT_FAILURE_LIMIT:
            return None
        return (
            f"RepeatedToolFailureError(tool={name!r}, consecutive_failures={count}, "
            f"last_error={last_error!r}, status='tool_failed', "
            "message='tool call skipped after repeated transient failures; "
            "return structured blocker evidence instead of retrying broad variants')"
        )

    def _record_tool_failure(self, name: str, error_text: str) -> None:
        """Track consecutive transient failures for bounded tool retries."""

        with self._failure_lock:
            if not _is_transient_tool_error(error_text):
                self._consecutive_transient_failures.pop(name, None)
                return
            count, _last_error = self._consecutive_transient_failures.get(name, (0, ""))
            self._consecutive_transient_failures[name] = (count + 1, error_text)

    def _record_tool_success(self, name: str) -> None:
        """Clear repeated-failure state after a successful tool call."""

        with self._failure_lock:
            self._consecutive_transient_failures.pop(name, None)

    def _timeout_for_tool(self, name: str) -> float:
        """Return the effective timeout for a single tool invocation."""

        return self._tool_timeouts.get(name, self._timeout)

    def get_tool_names(self) -> list[str]:
        """Return names of all available tools."""
        return self._async_executor.get_tool_names()

    def get_all_tool_definitions(self) -> dict[str, Any]:
        """Return all definitions, including app-only tools."""

        return self._async_executor.get_all_tool_definitions()

    def to_dspy_tools(self) -> list[dspy.Tool]:
        """Convert MCP tools to DSPy Tool objects."""
        return _make_dspy_tools(self._async_executor.get_tool_definitions(), self.call_tool)

    def close(self) -> None:
        """Shut down the executor, closing the client and event loop."""
        with self._close_lock:
            if self._closed:
                return
            self._closed = True

            if self._loop.is_running():
                try:
                    self._run_coroutine(
                        self._async_executor.aclose(),
                        timeout=min(5.0, max(0.1, self._timeout)),
                        action="MCP executor close",
                    )
                except TimeoutError:
                    logger.warning("Timed out closing SyncMCPToolExecutor client")
                except Exception as exc:  # noqa: BLE001 - client-close error logged at debug; teardown continues
                    logger.debug("Error closing SyncMCPToolExecutor client: %s", exc)

            if not self._loop.is_closed():
                with suppress(RuntimeError):
                    self._loop.call_soon_threadsafe(self._loop.stop)

        if threading.current_thread() is not self._thread:
            self._thread.join(timeout=5)

    def _run_coroutine(self, coro: Any, *, timeout: float, action: str) -> Any:
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            return future.result(timeout=timeout)
        except concurrent.futures.TimeoutError as exc:
            if future.done():
                raise
            future.cancel()
            raise TimeoutError(f"{action} timed out after {timeout:g}s") from exc


class MCPToolBridge(SyncMCPToolExecutor):
    """Backward-compatible name for the sync MCP tool executor."""


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


_FILE_ARGUMENT_NAMES = {
    "file",
    "filepath",
    "file_path",
    "path",
    "input",
    "input_path",
    "source",
    "source_path",
}

# Output-artifact argument names. When a tool writes a deliverable (a plot, an
# export, a report) it takes the destination via one of these args. Models vary
# in whether they emit an ABSOLUTE destination: stronger models obey the
# "pass an absolute path" prompt, weaker ones emit a bare filename (or omit the
# arg entirely and let the tool's own relative default apply). Either way the
# artifact then lands in the MCP server's CWD instead of the bound workspace,
# where the harness/grader collects deliverables. Grounding these against the
# active workspace root is generic workspace hygiene — it applies to every tool
# and every model, with no per-model or per-tool special-casing.
_OUTPUT_PATH_ARG_NAMES = {
    "output_path",
    "out_path",
    "output_file",
    "outfile",
    "output",
    "save_path",
    "savepath",
    "dest",
    "destination",
    "dest_path",
    "out",
}

_ARTIFACT_SUFFIXES = {
    ".png",
    ".jpg",
    ".jpeg",
    ".svg",
    ".pdf",
    ".gif",
    ".csv",
    ".tsv",
    ".parquet",
    ".json",
    ".html",
    ".txt",
    ".md",
    ".nc",
    ".h5",
    ".hdf5",
    ".npy",
    ".npz",
    ".xlsx",
}


def _is_relative_artifact_path(value: str) -> bool:
    """Return whether a string is a relative path that names a writable file."""

    candidate = value.strip()
    if not candidate:
        return False
    expanded = Path(candidate).expanduser()
    if expanded.is_absolute():
        return False
    # A bare scheme/URL is not a local filesystem destination.
    if "://" in candidate:
        return False
    return True


def _schema_properties(input_schema: Any) -> dict[str, Any]:
    """Return the ``properties`` mapping from an MCP tool inputSchema."""

    if not isinstance(input_schema, Mapping):
        return {}
    properties = input_schema.get("properties")
    if not isinstance(properties, Mapping):
        return {}
    return dict(properties)


def _ground_output_paths(
    args: Mapping[str, Any],
    input_schema: Any,
    workspace_root: str,
) -> dict[str, Any]:
    """Ground tool output-artifact paths against the active workspace root.

    Two model-agnostic repairs, both gated on a bound workspace root:

    1. RESOLVE a relative output path the model EMITS (e.g. ``"plot.png"``)
       against the workspace root so the deliverable lands where the harness
       collects it, instead of in the MCP server's process CWD.
    2. INJECT a workspace-absolute output path when the model OMITS an output
       arg whose schema declares a *relative* default (e.g. plot tools default
       ``output_path="timeseries.png"``). Without this the MCP server applies
       its own relative default inside the server, after this boundary runs.

    Absolute paths the model already supplied are left untouched. No per-model
    or per-tool branches: the only inputs are generic output-arg names and the
    tool's own declared inputSchema.
    """

    grounded = dict(args)
    root = workspace_root.strip()
    if not root:
        return grounded
    root_path = Path(root).expanduser()

    # (1) Resolve relative output paths the model emitted.
    for key, value in list(grounded.items()):
        if key not in _OUTPUT_PATH_ARG_NAMES:
            continue
        if not isinstance(value, str) or not _is_relative_artifact_path(value):
            continue
        grounded[key] = str(root_path / Path(value.strip()))

    # (2) Inject a workspace-absolute path for omitted output args whose schema
    #     default is relative (the tool would otherwise write to its own CWD).
    properties = _schema_properties(input_schema)
    for prop_name, prop_schema in properties.items():
        if prop_name not in _OUTPUT_PATH_ARG_NAMES or prop_name in grounded:
            continue
        if not isinstance(prop_schema, Mapping):
            continue
        default = prop_schema.get("default")
        if not isinstance(default, str):
            continue
        default_name = Path(default.strip()).name
        if not default_name:
            continue
        if Path(default_name).suffix.lower() not in _ARTIFACT_SUFFIXES:
            continue
        if not _is_relative_artifact_path(default):
            continue
        grounded[prop_name] = str(root_path / default_name)

    return grounded


# Bounds on the allowed-root basename scan: a mistyped path must not turn a tool
# call into an unbounded filesystem walk. Both are hard ceilings — hitting either
# aborts the scan and leaves the argument UNCHANGED, because a partial scan cannot
# prove a match is unique.
_REPAIR_SCAN_LIMIT = 20_000
_REPAIR_DEADLINE_S = 2.0


def _bounded_basename_matches(
    roots: Sequence[Path],
    basename: str,
    scanned: int,
    deadline: float,
) -> tuple[list[Path], int, bool]:
    """Walk ``roots`` for files named ``basename``, bounding every entry visited.

    Unlike ``Path.rglob``, which only yields name-matches (so a no-match basename
    over a huge tree would traverse it exhaustively before any bound could be
    consulted), this walk increments ``scanned`` and checks the wall-clock
    ``deadline`` for EVERY directory entry visited. Directory symlinks are not
    followed, matching ``rglob``'s non-recursing behavior and avoiding cycles.

    Returns:
        ``(matches, scanned, aborted)``: resolved file matches (the walk stops
        after a second match, which already disproves uniqueness), the updated
        entry count, and whether a bound aborted the walk.
    """
    matches: list[Path] = []
    for root in roots:
        stack: list[str] = [str(root)]
        while stack:
            directory = stack.pop()
            try:
                entries = os.scandir(directory)
            except OSError:
                continue
            with entries:
                for entry in entries:
                    scanned += 1
                    if scanned > _REPAIR_SCAN_LIMIT or time.monotonic() > deadline:
                        return matches, scanned, True
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                        elif entry.name == basename and entry.is_file():
                            matches.append(Path(entry.path).resolve())
                            if len(matches) > 1:
                                return matches, scanned, False
                    except OSError:
                        continue
    return matches, scanned, False


def _repair_missing_file_arguments(
    args: Mapping[str, Any],
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """Repair obvious missing file-path typos to a unique allowed-root match.

    Model-generated tool calls occasionally mistype a directory component while
    preserving the target basename. Retrying a unique basename match under the
    configured allowed roots keeps the repair inside the existing file policy:
    no outside-root access, and no ambiguous guessing.

    The allowed-root walk is bounded (``_REPAIR_SCAN_LIMIT`` entries across roots,
    ``_REPAIR_DEADLINE_S`` seconds); exceeding either bound aborts the scan and
    leaves the argument unchanged, since a partial scan cannot prove uniqueness.

    Returns:
        The (possibly repaired) argument dict, and a list of substitution records
        ``{"argument", "requested", "used"}`` — one per actually-substituted
        argument — so the caller can surface every repair in the tool result.
    """

    repaired = dict(args)
    records: list[dict[str, str]] = []
    try:
        policy = FileAccessPolicy.from_env()
    except Exception as exc:  # noqa: BLE001 - degradation surfaced via structured log below
        logger.warning(
            "file-argument repair skipped: file policy unavailable "
            "reason=file_policy_unavailable error=%r",
            exc,
        )
        return repaired, records

    scanned = 0
    deadline = time.monotonic() + _REPAIR_DEADLINE_S
    for key, value in list(repaired.items()):
        if key not in _FILE_ARGUMENT_NAMES or not isinstance(value, str) or not value.strip():
            continue
        candidate = Path(value).expanduser()
        if candidate.exists():
            continue
        basename = candidate.name
        if not basename or basename in {".", ".."}:
            continue
        matches, scanned, aborted = _bounded_basename_matches(
            policy.allowed_roots, basename, scanned, deadline
        )
        if aborted:
            # A partial scan can't prove uniqueness — leave the argument as-is.
            continue
        unique = sorted(set(matches))
        if len(unique) == 1:
            used = str(unique[0])
            repaired[key] = used
            records.append({"argument": key, "requested": value, "used": used})
    return repaired, records


def _prepend_repair_notes(records: Sequence[Mapping[str, str]], result: str) -> str:
    """Prepend a human-readable ``[path-repair]`` note per substitution to ``result``.

    Every file-argument substitution the executor made is surfaced verbatim in the
    tool result the model reads back, so a silently-corrected path is never
    invisible — the repair is auditable in the trace and to the model itself.
    """
    notes = "".join(
        f"[path-repair] argument '{rec['argument']}': '{rec['requested']}' not found; "
        f"substituted unique match '{rec['used']}'\n"
        for rec in records
    )
    return f"{notes}\n{result}"


def _make_dspy_tools(
    mcp_tools: Mapping[str, Any],
    call_tool: Callable[[str, Mapping[str, Any]], str],
) -> list[dspy.Tool]:
    """Convert discovered MCP tool definitions to DSPy Tool objects."""
    return [
        _make_dspy_tool(name, mcp_tool, call_tool)
        for name, mcp_tool in mcp_tools.items()
        if _tool_visible_to_model(mcp_tool)
    ]


def _make_dspy_tool(
    name: str,
    mcp_tool: Any,
    call_tool: Callable[[str, Mapping[str, Any]], str],
) -> dspy.Tool:
    """Create a single DSPy Tool from an MCP tool definition."""
    description = getattr(mcp_tool, "description", None) or name

    def tool_fn(**kwargs: Any) -> str:
        return call_tool(name, kwargs)

    tool_fn.__name__ = name
    tool_fn.__doc__ = description

    schema = getattr(mcp_tool, "inputSchema", None) or {}
    properties = schema.get("properties", {}) if isinstance(schema, Mapping) else {}
    if not isinstance(properties, dict):
        properties = {}

    return dspy.Tool(
        func=tool_fn,
        name=name,
        desc=description,
        args=properties,
    )


__all__ = [
    "AsyncMCPToolExecutor",
    "AsyncToolExecutor",
    "MCPToolBridge",
    "RepeatedToolFailureError",
    "SyncMCPToolExecutor",
    "SyncToolExecutor",
    "ToolExecutor",
    "ToolRuntimeHooks",
    "UncertainMutatingToolOutcomeError",
    "create_async_tool_executor",
    "create_sync_tool_executor",
    "current_tool_runtime",
    "get_active_tool_workspace_root",
    "notify_global_tool_observer",
    "notify_tool_observer",
    "recorded_tool_runtime_reasons",
    "set_tool_runtime_fallback",
    "set_tool_runtime_resolver",
    "tool_workspace_context",
]
