"""Tool execution boundary for CLIO experts."""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import logging
import threading
from collections.abc import Callable, Mapping
from contextlib import suppress
from typing import Any, AsyncContextManager, Protocol

import dspy
from fastmcp import Client

logger = logging.getLogger(__name__)

ClientFactory = Callable[[Any], AsyncContextManager[Any]]


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
    ):
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        if setup_timeout <= 0:
            raise ValueError("setup_timeout must be positive")

        self._server = server
        self._timeout = timeout
        self._setup_timeout = setup_timeout
        self._client_factory = client_factory or Client
        self._client: Any | None = None
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
            self._client = self._client_factory(self._server)
            await self._client.__aenter__()
            tools = await self._client.list_tools()
            for tool in tools:
                self._mcp_tools[tool.name] = tool
        except BaseException as exc:
            self._setup_error = exc
        finally:
            self._setup_done.set()

    def call_tool(self, name: str, args: Mapping[str, Any]) -> str:
        """Call an MCP tool synchronously via the background event loop."""
        if self._closed:
            raise RuntimeError("MCPToolBridge is closed")
        if self._client is None:
            raise RuntimeError("MCP client not initialized")

        result = self._run_coroutine(
            self._client.call_tool(name, dict(args)),
            timeout=self._timeout,
            action=f"MCP tool {name!r}",
        )
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

            if self._client is not None and self._loop.is_running():
                try:
                    future = asyncio.run_coroutine_threadsafe(
                        self._client.__aexit__(None, None, None),
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
