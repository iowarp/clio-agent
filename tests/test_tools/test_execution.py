"""Tests for the tool execution boundary."""

from types import SimpleNamespace
from typing import Any

import pytest

from clio_agent.tools.execution import (
    AsyncMCPToolExecutor,
    MCPToolBridge,
    SyncMCPToolExecutor,
    create_async_tool_executor,
    create_sync_tool_executor,
)


class FakeClient:
    """Minimal async client shape used by MCP executors."""

    def __init__(self, *, delay: float = 0.0):
        self.delay = delay
        self.entered = False
        self.exited = False
        self.started_call = False

    async def __aenter__(self):
        self.entered = True
        return self

    async def __aexit__(self, exc_type, exc, tb):
        self.exited = True

    async def list_tools(self):
        return [
            SimpleNamespace(
                name="fake_echo",
                description="Echo a value.",
                inputSchema={"properties": {"value": {"type": "string"}}},
            )
        ]

    async def call_tool(self, name: str, args: dict[str, Any]):
        import asyncio

        self.started_call = True
        if self.delay:
            await asyncio.sleep(self.delay)
        return SimpleNamespace(data={"name": name, "args": args})


@pytest.mark.asyncio
async def test_async_mcp_tool_executor_uses_explicit_async_lifecycle():
    """Async executor should call tools without creating a sync bridge thread."""
    fake_client = FakeClient()
    executor = create_async_tool_executor(
        object(),
        timeout=1.0,
        client_factory=lambda _: fake_client,
    )

    assert isinstance(executor, AsyncMCPToolExecutor)
    assert executor.started is False

    async with executor:
        assert executor.started is True
        assert executor.get_tool_names() == ["fake_echo"]
        result = await executor.call_tool("fake_echo", {"value": "hello"})
        assert '"name": "fake_echo"' in result
        assert not hasattr(executor, "_thread")

    assert fake_client.exited is True
    assert executor.closed is True


@pytest.mark.asyncio
async def test_async_mcp_tool_executor_timeout_cancels_tool_call():
    """Async calls should honor the configured timeout and still clean up."""
    fake_client = FakeClient(delay=0.2)
    executor = AsyncMCPToolExecutor(
        object(),
        timeout=0.01,
        client_factory=lambda _: fake_client,
    )
    await executor.start()

    try:
        with pytest.raises(TimeoutError, match="timed out"):
            await executor.call_tool("fake_echo", {"value": "slow"})
        assert fake_client.started_call is True
    finally:
        await executor.aclose()

    assert fake_client.exited is True
    assert executor.closed is True


def test_sync_mcp_tool_executor_closes_client_and_loop():
    """close() should shut down the client and background loop idempotently."""
    fake_client = FakeClient()
    executor = create_sync_tool_executor(
        object(),
        timeout=1.0,
        client_factory=lambda _: fake_client,
    )
    assert isinstance(executor, SyncMCPToolExecutor)
    thread = executor._thread

    try:
        assert executor.get_tool_names() == ["fake_echo"]
        result = executor.call_tool("fake_echo", {"value": "hello"})
        assert '"name": "fake_echo"' in result
    finally:
        executor.close()

    executor.close()
    assert executor.closed is True
    assert fake_client.exited is True
    assert not thread.is_alive()


def test_sync_mcp_tool_executor_timeout_cancels_tool_call():
    """Tool calls should honor the configured timeout and still clean up."""
    fake_client = FakeClient(delay=0.2)
    executor = SyncMCPToolExecutor(
        object(),
        timeout=0.01,
        client_factory=lambda _: fake_client,
    )

    try:
        with pytest.raises(TimeoutError, match="timed out"):
            executor.call_tool("fake_echo", {"value": "slow"})
        assert fake_client.started_call is True
    finally:
        executor.close()

    assert fake_client.exited is True
    assert executor.closed is True


def test_mcp_tool_bridge_remains_sync_compatibility_shim():
    """The old bridge name should remain available without driving expert wiring."""
    fake_client = FakeClient()
    bridge = MCPToolBridge(object(), timeout=1.0, client_factory=lambda _: fake_client)

    try:
        assert isinstance(bridge, SyncMCPToolExecutor)
        assert bridge.call_tool("fake_echo", {"value": "hello"}).startswith("{")
    finally:
        bridge.close()
