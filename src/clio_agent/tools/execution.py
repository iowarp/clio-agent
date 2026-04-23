"""Tool execution boundaries for CLIO experts."""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import logging
import threading
from collections.abc import Callable, Mapping
from contextlib import suppress
from typing import Any, Protocol

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


def create_async_tool_executor(
    server: Any,
    *,
    timeout: float = 30.0,
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
        client_factory=client_factory,
    )


def create_sync_tool_executor(
    server: Any,
    *,
    timeout: float = 30.0,
    setup_timeout: float = 10.0,
    client_factory: ClientFactory | None = None,
) -> SyncToolExecutor:
    """Create a sync executor for CLI and deterministic expert call sites."""
    return SyncMCPToolExecutor(
        server,
        timeout=timeout,
        setup_timeout=setup_timeout,
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
        client_factory: ClientFactory | None = None,
    ) -> None:
        if timeout <= 0:
            raise ValueError("timeout must be positive")

        self._server = server
        self._timeout = timeout
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
            try:
                result = await asyncio.wait_for(
                    self._client.call_tool(name, dict(args)),
                    timeout=self._timeout,
                )
            except TimeoutError as exc:
                raise TimeoutError(
                    f"MCP tool {name!r} timed out after {self._timeout:g}s"
                ) from exc
        return _result_to_text(result)

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
        client_factory: ClientFactory | None = None,
    ):
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        if setup_timeout <= 0:
            raise ValueError("setup_timeout must be positive")

        self._timeout = timeout
        self._setup_timeout = setup_timeout
        self._async_executor = AsyncMCPToolExecutor(
            server,
            timeout=timeout,
            client_factory=client_factory,
        )
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
                self._loop.run_until_complete(
                    asyncio.gather(*pending, return_exceptions=True)
                )
            with suppress(Exception):
                self._loop.run_until_complete(self._loop.shutdown_asyncgens())
            self._loop.close()

    def call_tool(self, name: str, args: Mapping[str, Any]) -> str:
        """Call an MCP tool synchronously via the background event loop."""
        if self._closed:
            raise RuntimeError("SyncMCPToolExecutor is closed")
        return self._run_coroutine(
            self._async_executor.call_tool(name, args),
            timeout=self._timeout,
            action=f"MCP tool {name!r}",
        )

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


def _make_dspy_tools(
    mcp_tools: Mapping[str, Any],
    call_tool: Callable[[str, Mapping[str, Any]], str],
) -> list[dspy.Tool]:
    """Convert discovered MCP tool definitions to DSPy Tool objects."""
    return [
        _make_dspy_tool(name, mcp_tool, call_tool)
        for name, mcp_tool in mcp_tools.items()
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
    "SyncMCPToolExecutor",
    "SyncToolExecutor",
    "ToolExecutor",
    "create_async_tool_executor",
    "create_sync_tool_executor",
]
