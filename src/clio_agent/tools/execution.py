"""Tool execution boundaries for CLIO experts."""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextvars
import inspect
import json
import logging
import os
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Optional, Protocol

import dspy

from clio_agent import conf
from clio_agent.errors import ClioError
from clio_agent.runtime.stream_audit import stream_audit
from clio_agent.tools import foreground_cancellation as foreground_cancel
from clio_agent.tools.file_policy import FileAccessPolicy
from clio_agent.tools.mcp_executor import (
    AsyncMCPToolExecutor,
    ClientFactory,
    MCPClientProtocol,
    UncertainMutatingToolOutcomeError,
    _clean_tool_timeouts,
    _tool_annotations,
    _tool_input_schema,
    _tool_visible_to_model,
)
from clio_agent.tools.mcp_results import call_tool_result_to_observer
from clio_agent.tools.tool_hooks import InterceptDecision, PostToolHook, apply_post_tool_hook

logger = logging.getLogger(__name__)


# iowarp/clio-agent#7 + #2 + #735: the four tool-runtime hooks (permission
# gate, telemetry observer, preflight interceptor, cancellation checker) are
# resolved per tool call through the ``ToolRuntimeHooks`` seam below — gact
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
# This is the SOLE tool-hook mechanism: the resolver is the in-turn path and the #
# single ``_FALLBACK_TOOL_RUNTIME`` bundle is the reason-logged app-less net.    #


@dataclass(frozen=True)
class ToolRuntimeHooks:
    """The per-tool-call hook bundle resolved for the live turn (#735).

    A frozen value object carrying the tool-runtime hooks. ``None`` on any
    field means "no such hook" (a no-op), never "look elsewhere".
    """

    permission_gate: PermissionGate | None = None
    tool_observer: Optional[ToolObserver | LegacyToolObserver] = None
    tool_interceptor: Optional[Callable[[str, Mapping[str, Any]], "InterceptDecision | None"]] = (
        None
    )
    cancellation_checker: Optional[Callable[[], bool]] = None
    loop_inbox_drain: Optional[Callable[[], "str | None"]] = None  # #1035 injected gact drain
    # The ordinary observer gets only the sanitized public MCP projection; MCP Apps
    # need the full CallToolResult (private ``_meta``) held in a session-local store.
    mcp_app_observer: Optional[MCPAppObserver] = None
    # P2.3 PostToolUse: applied after a tool result (or a synthesized one) to
    # observe / rewrite the model-visible observation / feed a deny reason back.
    post_tool: Optional[PostToolHook] = None


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


# Marker on a tool CALLABLE whose execution path already reaches the observer
# (stamped by _make_dspy_tool here and by gact.agents.tool_instrumentation), so
# the default-on instrumentation seam never double-notifies a call.
TOOL_OBSERVED_ATTR = "_clio_tool_observed"


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

REPEATED_TRANSIENT_FAILURE_LIMIT = 2
SYNC_TOOL_RESULT_GRACE_SECONDS = 1.0


class RepeatedToolFailureError(RuntimeError):
    """Raised when a tool keeps failing with transient infrastructure errors."""


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
    server_id: str = "",
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
        server_id=server_id,
    )


def create_sync_tool_executor(
    server: Any,
    *,
    timeout: float | None = None,
    setup_timeout: float | None = None,
    tool_timeouts: Mapping[str, float] | None = None,
    client_factory: ClientFactory | None = None,
    preloaded_tools: Mapping[str, Any] | None = None,
    namespace_servers: Mapping[str, Any] | None = None,
    server_id: str = "",
) -> SyncToolExecutor:
    """Create a sync executor for CLI and deterministic expert call sites."""
    # #1186 follow-on: the per-call ceiling is config-resolved like setup_timeout.
    # 30s starves real scientific tools (a 50MB staged CSV made plot_plot_timeseries
    # time out regardless of row caps); deployments size this to their data.
    effective_timeout = (
        conf.resolve(
            "tools.mcp.call_timeout_s",
            env="CLIO_MCP_CALL_TIMEOUT_S",
            default=30.0,
            cast=conf.as_float,
        )
        if timeout is None
        else timeout
    )
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
        timeout=effective_timeout,
        setup_timeout=effective_setup_timeout,
        tool_timeouts=tool_timeouts,
        client_factory=client_factory,
        preloaded_tools=preloaded_tools,
        namespace_servers=namespace_servers,
        server_id=server_id,
    )


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
        server_id: str = "",
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
            server_id=server_id,
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

        Two optional injection points fire around the underlying FastMCP call:
          1. ``permission_gate(name, args) -> {"allow"|"deny"}`` —
             when configured, runs first. "deny" raises PermissionError;
             the ReAct loop reports it back as the assistant answer.
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
                raise foreground_cancel._tool_cancellation_error(name, stage)

        effective_args, repair_records = _repair_missing_file_arguments(args)
        effective_args = _ground_output_paths(
            effective_args,
            _tool_input_schema(self._mcp_tools.get(name)),  # fastmcp-4 snake-case-aware read
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
                # P1.2 #1064: surface a mode-aware ``deny_message`` (a ``str`` subclass; duck-typed
                # via getattr so this layer imports no gact) instead of the generic string; a plain
                # "deny" falls back. The typed audit reason (``policy_deny``) is unchanged.
                deny_message = getattr(decision, "deny_message", "")
                raise PermissionError(
                    deny_message or f"tool call {name!r} denied by permission gate"
                )

        raise_if_cancelled("tool_call_before")

        circuit_error = self._repeated_transient_failure_error(name)
        if circuit_error is not None:
            notify_tool_observer(tool_observer, name, effective_args, "started", None)
            notify_tool_observer(tool_observer, name, effective_args, "completed", circuit_error)
            raise RepeatedToolFailureError(circuit_error)

        # P2.3: gate-stashed, single-fire PreToolUse decision. ``modify`` mutates input;
        # ``synthesize`` skips the call for a fabricated result (no PostToolUse if raw).
        intercept = (
            hooks.tool_interceptor(name, dict(effective_args)) if hooks.tool_interceptor else None
        )
        if (
            intercept is not None
            and intercept.kind == "modify"
            and intercept.modified_args is not None
        ):
            effective_args = dict(intercept.modified_args)
        elif intercept is not None and intercept.kind == "synthesize":
            notify_tool_observer(tool_observer, name, effective_args, "started", None)
            notify_tool_observer(
                tool_observer, name, effective_args, "completed", None, intercept.result
            )
            self._record_tool_success(name)
            if return_raw:
                return intercept.result  # MCP Apps bridge is not model-facing: no PostToolUse
            return apply_post_tool_hook(
                hooks.post_tool,
                name,
                effective_args,
                intercept.result,
                is_error=False,
                synthetic=True,
            )

        notify_tool_observer(tool_observer, name, effective_args, "started", None)

        budget = self._async_executor._timeout_budget_for_call(name, effective_args)
        timeout = budget.seconds
        try:
            outcome = foreground_cancel._run_foreground_coroutine(
                self._loop,
                self._async_executor.call_tool_result(name, effective_args),
                timeout=timeout + SYNC_TOOL_RESULT_GRACE_SECONDS,
                action=f"MCP tool {name!r}",
                cancellation_checker=cancellation_checker,
                cancellation_error=lambda wire_settled: foreground_cancel._tool_cancellation_error(
                    name,
                    "tool_call_in_flight",
                    wire_settled=wire_settled,
                ),
            )
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
            trace = {"error": exc.to_dict()} if isinstance(exc, ClioError) else None
            notify_tool_observer(
                tool_observer, name, effective_args, "completed", error_text, trace
            )
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
            return outcome.raw_result  # MCP Apps bridge is not model-facing: no PostToolUse
        drained = hooks.loop_inbox_drain() if hooks.loop_inbox_drain is not None else None
        result = _prepend_repair_notes(repair_records, result) if repair_records else result
        # P2.3 PostToolUse: rewrite the model-visible observation / feed a deny reason,
        # AFTER the observer recorded the real effect (trace keeps the actual result).
        result = apply_post_tool_hook(
            hooks.post_tool,
            name,
            effective_args,
            result,
            is_error=structured_error is not None,
            synthetic=False,
        )
        return f"{result}\n\n{drained}" if drained else result

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

    def namespaces(self) -> tuple[str, ...]:
        """Declared server namespaces this executor routes to (#1201 gact readers)."""
        return self._async_executor.namespaces()

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

# Output-artifact designation table (issue #966 deletion inventory item 2): the
# tool-declared output-arg names, artifact suffixes and the pre-call grounding
# now live in the artifacts designation module — the ONE place that decides which
# output paths become artifacts (designation, not discovery). The grounding call
# in ``_call_tool_inner`` imports :func:`ground_output_paths` from there LAZILY:
# ``execution.py`` is imported DURING ``clio_agent.gact`` package init (app.py ->
# execution), so a top-level ``from clio_agent.gact.artifacts...`` would re-enter
# the half-initialized ``gact`` package and deadlock the import. The lazy import
# keeps the tool boundary's behavior byte-identical (parity test) with no cycle.


def _ground_output_paths(
    args: "Mapping[str, Any]",
    input_schema: Any,
    workspace_root: str,
) -> dict[str, Any]:
    """Thin re-export of the artifacts designation grounding (behavior unchanged)."""
    from clio_agent.gact.artifacts.designation import ground_output_paths  # noqa: PLC0415

    return ground_output_paths(args, input_schema, workspace_root)


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
    # Bridged calls notify inside call_tool (this boundary): mark the callable
    # so the instrumentation seam never adds a second notification.
    setattr(tool_fn, TOOL_OBSERVED_ATTR, True)
    # #1188 MCP half; owner logic in tool_instrumentation (lazy: cross-package cycle).
    from clio_agent.gact.agents.tool_instrumentation import stamp_mcp_tool_title  # noqa: PLC0415

    stamp_mcp_tool_title(tool_fn, mcp_tool)

    properties = _tool_input_schema(mcp_tool).get("properties", {})  # fastmcp-4 snake read
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
    "ClientFactory",
    "MCPClientProtocol",
    "MCPToolBridge",
    "RepeatedToolFailureError",
    "SyncMCPToolExecutor",
    "SyncToolExecutor",
    "TOOL_OBSERVED_ATTR",
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
