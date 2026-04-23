"""Tests for the tool execution boundary."""

from types import SimpleNamespace
from typing import Any

import pytest

from clio_agent.tools.execution import MCPToolBridge


class FakeClient:
    """Minimal async client shape used by MCPToolBridge."""

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


def test_mcp_tool_bridge_closes_client_and_loop():
    """close() should shut down the client and background loop idempotently."""
    fake_client = FakeClient()
    bridge = MCPToolBridge(object(), timeout=1.0, client_factory=lambda _: fake_client)
    thread = bridge._thread

    try:
        assert bridge.get_tool_names() == ["fake_echo"]
        result = bridge.call_tool("fake_echo", {"value": "hello"})
        assert '"name": "fake_echo"' in result
    finally:
        bridge.close()

    bridge.close()
    assert bridge.closed is True
    assert fake_client.exited is True
    assert not thread.is_alive()


def test_mcp_tool_bridge_timeout_cancels_tool_call():
    """Tool calls should honor the configured timeout and still clean up."""
    fake_client = FakeClient(delay=0.2)
    bridge = MCPToolBridge(object(), timeout=0.01, client_factory=lambda _: fake_client)

    try:
        with pytest.raises(TimeoutError, match="timed out"):
            bridge.call_tool("fake_echo", {"value": "slow"})
        assert fake_client.started_call is True
    finally:
        bridge.close()

    assert fake_client.exited is True
    assert bridge.closed is True
