"""Tool execution boundary for CLIO experts."""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import logging
import threading
from collections.abc import Callable, Mapping
from contextlib import suppress
from typing import Any, Optional, Protocol

import dspy
from fastmcp import Client

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
# (or any other harness) sets these once and every MCPToolBridge
# constructed thereafter picks them up. None means "no-op".
_GLOBAL_PERMISSION_GATE: Optional[
    Callable[[str, Mapping[str, Any]], str]
] = None
_GLOBAL_TOOL_OBSERVER: Optional[
    Callable[[str, Mapping[str, Any], Optional[str], Optional[str]], None]
] = None


def set_global_permission_gate(
    gate: Optional[Callable[[str, Mapping[str, Any]], str]],
) -> None:
    """Install a process-global permission gate. Pass None to disable."""

    global _GLOBAL_PERMISSION_GATE
    _GLOBAL_PERMISSION_GATE = gate


def set_global_tool_observer(
    observer: Optional[
        Callable[[str, Mapping[str, Any], Optional[str], Optional[str]], None]
    ],
) -> None:
    """Install a process-global tool-call observer. Pass None to disable."""

    global _GLOBAL_TOOL_OBSERVER
    _GLOBAL_TOOL_OBSERVER = observer


class ToolExecutor(Protocol):
    """Synchronous tool execution interface used by ReAct experts."""

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


class MCPToolBridge:
    """Bridge async FastMCP tools into synchronous DSPy tool calls.

    The bridge owns a background event loop and a single FastMCP client
    connection. Experts depend on the ToolExecutor protocol, so future async or
    API-native execution paths can replace this implementation without
    rewriting expert logic.
    """

    def __init__(
        self,
        server: Any,
        timeout: float = 30.0,
        setup_timeout: float = 10.0,
        client_factory: ClientFactory | None = None,
        permission_gate: Optional[Callable[[str, Mapping[str, Any]], str]] = None,
        tool_observer: Optional[
            Callable[[str, Mapping[str, Any], Optional[str], Optional[str]], None]
        ] = None,
    ):
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        if setup_timeout <= 0:
            raise ValueError("setup_timeout must be positive")

        self._server = server
        self._timeout = timeout
        self._setup_timeout = setup_timeout
        self._client_factory = client_factory or Client
        # iowarp/clio-agent#7: optional gate called BEFORE every
        # tool invocation. Returns one of:
        #   "allow"  → run the tool unchanged
        #   "deny"   → raise a PermissionError; the agent sees the
        #              traceback in its tool_result and reports it.
        # Defaults to the module-level _GLOBAL_PERMISSION_GATE so the
        # GACT layer can wire a single check across every expert at
        # startup without monkey-patching individual bridges.
        self._permission_gate = permission_gate or _GLOBAL_PERMISSION_GATE
        # iowarp/clio-agent#2: optional observer called BEFORE
        # ("started") and AFTER ("completed", error?) every tool
        # invocation. Same global-fallback story.
        self._tool_observer = tool_observer or _GLOBAL_TOOL_OBSERVER
        self._client_ctx: MCPClientProtocol | None = None
        self._client: MCPClientProtocol | None = None
        self._mcp_tools: dict[str, Any] = {}
        self._setup_done = threading.Event()
        self._setup_error: BaseException | None = None
        self._closed = False
        self._close_lock = threading.Lock()

        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._run_loop,
            name="clio-mcp-tool-bridge",
            daemon=True,
        )
        self._thread.start()
        self._loop.call_soon_threadsafe(
            lambda: self._loop.create_task(self._setup())
        )

        if not self._setup_done.wait(timeout=setup_timeout):
            self.close()
            raise TimeoutError(f"MCPToolBridge setup timed out after {setup_timeout:g}s")
        if self._setup_error is not None:
            error = self._setup_error
            self.close()
            raise RuntimeError(f"MCPToolBridge setup failed: {error}") from error

    @property
    def closed(self) -> bool:
        """Return whether the bridge has been closed."""
        return self._closed

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
                self._loop.run_until_complete(
                    asyncio.gather(*pending, return_exceptions=True)
                )
            with suppress(Exception):
                self._loop.run_until_complete(self._loop.shutdown_asyncgens())
            self._loop.close()

    async def _setup(self) -> None:
        """Open the client connection and discover tools."""
        try:
            self._client_ctx = self._client_factory(self._server)
            self._client = await self._client_ctx.__aenter__()
            tools = await self._client.list_tools()
            for tool in tools:
                self._mcp_tools[tool.name] = tool
        except BaseException as exc:
            self._setup_error = exc
        finally:
            self._setup_done.set()

    def call_tool(self, name: str, args: Mapping[str, Any]) -> str:
        """Call an MCP tool synchronously via the background event loop.

        Two optional injection points fire around the underlying
        FastMCP call:
          1. ``permission_gate(name, args) -> {"allow"|"deny"}`` —
             when configured, runs first. "deny" raises
             PermissionError; the ReAct loop sees the traceback in
             the tool_result and reports it back as the assistant
             answer.
          2. ``tool_observer(name, args, phase, error?)`` —
             non-blocking notifications of "started" + "completed"
             so the GACT layer can publish tool.call.* events.
        """

        if self._closed:
            raise RuntimeError("MCPToolBridge is closed")
        if self._client is None:
            raise RuntimeError("MCP client not initialized")

        if self._permission_gate is not None:
            try:
                decision = self._permission_gate(name, dict(args))
            except Exception as exc:  # noqa: BLE001
                raise PermissionError(
                    f"permission gate raised: {exc!r}"
                ) from exc
            if decision != "allow":
                raise PermissionError(
                    f"tool call {name!r} denied by permission gate"
                )

        if self._tool_observer is not None:
            try:
                self._tool_observer(name, dict(args), "started", None)
            except Exception:
                pass

        try:
            result = self._run_coroutine(
                self._client.call_tool(name, dict(args)),
                timeout=self._timeout,
                action=f"MCP tool {name!r}",
            )
        except Exception as exc:
            if self._tool_observer is not None:
                try:
                    self._tool_observer(name, dict(args), "completed", repr(exc))
                except Exception:
                    pass
            raise
        if self._tool_observer is not None:
            try:
                self._tool_observer(name, dict(args), "completed", None)
            except Exception:
                pass

        data = result.data
        if isinstance(data, dict):
            return json.dumps(data)
        return str(data)

    def get_tool_names(self) -> list[str]:
        """Return names of all available tools."""
        return list(self._mcp_tools.keys())

    def to_dspy_tools(self) -> list[dspy.Tool]:
        """Convert MCP tools to DSPy Tool objects."""
        return [
            self._make_dspy_tool(name, mcp_tool)
            for name, mcp_tool in self._mcp_tools.items()
        ]

    def _make_dspy_tool(self, name: str, mcp_tool: Any) -> dspy.Tool:
        """Create a single DSPy Tool from an MCP tool definition."""
        description = mcp_tool.description or name

        def tool_fn(**kwargs: Any) -> str:
            return self.call_tool(name, kwargs)

        tool_fn.__name__ = name
        tool_fn.__doc__ = description

        schema = mcp_tool.inputSchema or {}
        properties = schema.get("properties", {})

        return dspy.Tool(
            func=tool_fn,
            name=name,
            desc=description,
            args=properties,
        )

    def close(self) -> None:
        """Shut down the bridge, closing the client and event loop."""
        with self._close_lock:
            if self._closed:
                return
            self._closed = True

            if self._client_ctx is not None and self._loop.is_running():
                try:
                    future = asyncio.run_coroutine_threadsafe(
                        self._client_ctx.__aexit__(None, None, None),
                        self._loop,
                    )
                    future.result(timeout=min(5.0, max(0.1, self._timeout)))
                except concurrent.futures.TimeoutError:
                    future.cancel()
                    logger.warning("Timed out closing MCPToolBridge client")
                except Exception as exc:
                    logger.debug("Error closing MCPToolBridge client: %s", exc)

            if self._loop.is_running():
                self._loop.call_soon_threadsafe(self._loop.stop)

        if threading.current_thread() is not self._thread:
            self._thread.join(timeout=5)

    def _run_coroutine(self, coro: Any, *, timeout: float, action: str) -> Any:
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            return future.result(timeout=timeout)
        except concurrent.futures.TimeoutError as exc:
            future.cancel()
            raise TimeoutError(f"{action} timed out after {timeout:g}s") from exc
