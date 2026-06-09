"""Tool execution boundaries for CLIO experts."""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextvars
import json
import logging
import threading
from collections.abc import Callable, Mapping
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Any, Iterator, Optional, Protocol

import dspy
from fastmcp import Client

from clio_agent.errors import CancellationError
from clio_agent.tools.file_policy import FileAccessPolicy

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


ClientFactory = Callable[[Any], MCPClientProtocol]


# iowarp/clio-agent#7 + #2: process-global hooks. The GACT layer
# (or any other harness) sets these once and every SyncMCPToolExecutor
# consults them at call time. None means "no-op".
_GLOBAL_PERMISSION_GATE: Optional[Callable[[str, Mapping[str, Any]], str]] = None
_GLOBAL_TOOL_INTERCEPTOR: Optional[Callable[[str, Mapping[str, Any]], Any | None]] = None
ToolObserver = Callable[
    [str, Mapping[str, Any], Optional[str], Optional[str], Any | None],
    None,
]
LegacyToolObserver = Callable[[str, Mapping[str, Any], Optional[str], Optional[str]], None]

_GLOBAL_TOOL_OBSERVER: Optional[ToolObserver | LegacyToolObserver] = None
_GLOBAL_CANCELLATION_CHECKER: Optional[Callable[[], bool]] = None
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


def set_global_permission_gate(
    gate: Optional[Callable[[str, Mapping[str, Any]], str]],
) -> None:
    """Install a process-global permission gate. Pass None to disable."""

    global _GLOBAL_PERMISSION_GATE
    _GLOBAL_PERMISSION_GATE = gate


def set_global_tool_observer(
    observer: Optional[ToolObserver | LegacyToolObserver],
) -> None:
    """Install a process-global tool-call observer. Pass None to disable."""

    global _GLOBAL_TOOL_OBSERVER
    _GLOBAL_TOOL_OBSERVER = observer


def set_global_tool_interceptor(
    interceptor: Optional[Callable[[str, Mapping[str, Any]], Any | None]],
) -> None:
    """Install a process-global preflight tool interceptor. Pass None to disable."""

    global _GLOBAL_TOOL_INTERCEPTOR
    _GLOBAL_TOOL_INTERCEPTOR = interceptor


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
            observer(name, dict(args), phase, error)  # type: ignore[misc]
        else:
            try:
                observer(name, dict(args), phase, error, result)  # type: ignore[misc]
            except TypeError:
                observer(name, dict(args), phase, error)  # type: ignore[misc]
    except Exception:
        pass


def notify_global_tool_observer(
    name: str,
    args: Mapping[str, Any],
    phase: str,
    error: str | None = None,
    result: Any | None = None,
) -> None:
    """Notify the process-global tool observer, swallowing observer failures."""

    notify_tool_observer(_GLOBAL_TOOL_OBSERVER, name, args, phase, error, result)


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


def set_global_cancellation_checker(checker: Optional[Callable[[], bool]]) -> None:
    """Install a process-global cooperative cancellation checker."""

    global _GLOBAL_CANCELLATION_CHECKER
    _GLOBAL_CANCELLATION_CHECKER = checker


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


class RepeatedToolFailureError(RuntimeError):
    """Raised when a tool keeps failing with transient infrastructure errors."""


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
    )


def create_sync_tool_executor(
    server: Any,
    *,
    timeout: float = 30.0,
    setup_timeout: float = 10.0,
    tool_timeouts: Mapping[str, float] | None = None,
    client_factory: ClientFactory | None = None,
) -> SyncToolExecutor:
    """Create a sync executor for CLI and deterministic expert call sites."""
    return SyncMCPToolExecutor(
        server,
        timeout=timeout,
        setup_timeout=setup_timeout,
        tool_timeouts=tool_timeouts,
        client_factory=client_factory,
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
    ) -> None:
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        cleaned_tool_timeouts = _clean_tool_timeouts(tool_timeouts)

        self._server = server
        self._timeout = timeout
        self._tool_timeouts = cleaned_tool_timeouts
        self._client_factory = client_factory or Client
        self._client_ctx: MCPClientProtocol | None = None
        self._client: MCPClientProtocol | None = None
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
        if self._closed:
            raise RuntimeError("AsyncMCPToolExecutor is closed")
        if self._client is None or self._call_lock is None:
            raise RuntimeError("AsyncMCPToolExecutor is not started")

        async with self._call_lock:
            timeout = self._timeout_for_tool(name)
            try:
                result = await asyncio.wait_for(
                    self._client.call_tool(name, dict(args)),
                    timeout=timeout,
                )
            except TimeoutError as exc:
                raise TimeoutError(f"MCP tool {name!r} timed out after {timeout:g}s") from exc
        return _result_to_text(result)

    def _timeout_for_tool(self, name: str) -> float:
        """Return the effective timeout for a single tool invocation."""

        return self._tool_timeouts.get(name, self._timeout)

    def get_tool_names(self) -> list[str]:
        """Return names of all discovered tools."""
        return list(self._mcp_tools.keys())

    def get_tool_definitions(self) -> dict[str, Any]:
        """Return discovered MCP tool definitions keyed by stable tool name."""
        return dict(self._mcp_tools)

    async def aclose(self) -> None:
        """Close the client connection."""
        if self._closed:
            return
        self._closed = True

        if self._client_ctx is not None:
            close_timeout = min(5.0, max(0.1, self._timeout))
            try:
                await asyncio.wait_for(
                    self._client_ctx.__aexit__(None, None, None),
                    timeout=close_timeout,
                )
            except Exception as exc:
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
        permission_gate: Optional[Callable[[str, Mapping[str, Any]], str]] = None,
        tool_observer: Optional[ToolObserver | LegacyToolObserver] = None,
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
            tool_timeouts=cleaned_tool_timeouts,
            client_factory=client_factory,
        )
        # iowarp/clio-agent#7: optional gate called BEFORE every
        # tool invocation. Returns one of:
        #   "allow"  → run the tool unchanged
        #   "deny"   → raise a PermissionError; the agent sees the
        #              traceback in its tool_result and reports it.
        # Explicit instance hook wins. When omitted, call_tool consults
        # the module-level _GLOBAL_PERMISSION_GATE dynamically so GACT
        # deferred startup can wire hooks after an executor exists.
        self._permission_gate = permission_gate
        # iowarp/clio-agent#2: optional observer called BEFORE
        # ("started") and AFTER ("completed", error?) every tool
        # invocation. Same global-fallback story.
        self._tool_observer = tool_observer
        self._failure_lock = threading.Lock()
        self._consecutive_transient_failures: dict[str, tuple[int, str]] = {}
        self._closed = False
        self._close_lock = threading.Lock()

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

        permission_gate = self._permission_gate or _GLOBAL_PERMISSION_GATE
        tool_observer = self._tool_observer or _GLOBAL_TOOL_OBSERVER
        cancellation_checker = _GLOBAL_CANCELLATION_CHECKER

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

        effective_args = _repair_missing_file_arguments(args)

        if permission_gate is not None:
            try:
                decision = permission_gate(name, dict(effective_args))
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

        tool_interceptor = _GLOBAL_TOOL_INTERCEPTOR
        if tool_interceptor is not None:
            intercepted = tool_interceptor(name, dict(effective_args))
            if intercepted is not None:
                notify_tool_observer(tool_observer, name, effective_args, "started", None)
                notify_tool_observer(
                    tool_observer, name, effective_args, "completed", None, intercepted
                )
                return intercepted

        notify_tool_observer(tool_observer, name, effective_args, "started", None)

        try:
            timeout = self._timeout_for_tool(name)
            result = self._run_coroutine(
                self._async_executor.call_tool(name, effective_args),
                timeout=timeout,
                action=f"MCP tool {name!r}",
            )
            raise_if_cancelled("tool_call_after")
        except Exception as exc:
            error_text = repr(exc)
            self._record_tool_failure(name, error_text)
            notify_tool_observer(tool_observer, name, effective_args, "completed", error_text)
            raise
        structured_error = _structured_tool_result_error(result)
        if structured_error:
            self._record_tool_failure(name, structured_error)
            notify_tool_observer(
                tool_observer,
                name,
                effective_args,
                "completed",
                structured_error,
                result,
            )
        else:
            self._record_tool_success(name)
            notify_tool_observer(tool_observer, name, effective_args, "completed", None, result)

        return result

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
                except Exception as exc:
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


def _repair_missing_file_arguments(args: Mapping[str, Any]) -> dict[str, Any]:
    """Repair obvious missing file-path typos to a unique allowed-root match.

    Model-generated tool calls occasionally mistype a directory component while
    preserving the target basename. Retrying a unique basename match under the
    configured allowed roots keeps the repair inside the existing file policy:
    no outside-root access, and no ambiguous guessing.
    """

    repaired = dict(args)
    try:
        policy = FileAccessPolicy.from_env()
    except Exception:
        return repaired

    for key, value in list(repaired.items()):
        if key not in _FILE_ARGUMENT_NAMES or not isinstance(value, str) or not value.strip():
            continue
        candidate = Path(value).expanduser()
        if candidate.exists():
            continue
        basename = candidate.name
        if not basename or basename in {".", ".."}:
            continue
        matches: list[Path] = []
        for root in policy.allowed_roots:
            try:
                for found in root.rglob(basename):
                    if found.is_file():
                        matches.append(found.resolve())
                        if len(matches) > 1:
                            break
            except OSError:
                continue
            if len(matches) > 1:
                break
        unique = sorted(set(matches))
        if len(unique) == 1:
            repaired[key] = str(unique[0])
    return repaired


def _make_dspy_tools(
    mcp_tools: Mapping[str, Any],
    call_tool: Callable[[str, Mapping[str, Any]], str],
) -> list[dspy.Tool]:
    """Convert discovered MCP tool definitions to DSPy Tool objects."""
    return [_make_dspy_tool(name, mcp_tool, call_tool) for name, mcp_tool in mcp_tools.items()]


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
    "create_async_tool_executor",
    "create_sync_tool_executor",
    "notify_global_tool_observer",
    "notify_tool_observer",
    "set_global_cancellation_checker",
    "set_global_permission_gate",
    "set_global_tool_interceptor",
    "set_global_tool_observer",
    "tool_workspace_context",
]
