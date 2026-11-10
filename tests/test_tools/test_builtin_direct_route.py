"""A built-in call reaches its own in-process server, never the composite gateway.

The composite resolved a built-in name by listing every mounted server, starting each
declared server not yet running: a session's first ``fs_list_directory`` waited on the
``web`` server's start (X1, 2026-11).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import mcp_types
import pytest
from fastmcp import FastMCP

from clio_agent.gact import context
from clio_agent.tools.mcp_executor import AsyncMCPToolExecutor
from clio_agent.tools.servers.fs_server import fs_server


class _RecordingTasks:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any, str]] = []

    async def call(
        self, server: str, configured: Any, tool: str, args: dict[str, Any], **_: Any
    ) -> Any:
        self.calls.append((server, configured, tool))
        return mcp_types.CallToolResult(content=[mcp_types.TextContent(type="text", text="ok")])


@pytest.mark.asyncio
async def test_a_builtin_call_goes_to_its_own_server() -> None:
    """SABOTAGE: drop the ``_builtin_namespace_server`` lookup and the call goes to the
    gateway under its full name."""
    gateway = FastMCP("gw")
    tasks = _RecordingTasks()
    built: list[Any] = []

    def factory(server: Any) -> Any:
        built.append(server)
        return SimpleNamespace(transport=object(), server=server)

    executor = AsyncMCPToolExecutor(
        gateway,
        client_factory=factory,
        preloaded_tools={"fs_list_directory": SimpleNamespace(name="fs_list_directory")},
    )
    executor._mcp_tools = {"fs_list_directory": SimpleNamespace(name="fs_list_directory")}
    app = SimpleNamespace(
        state=SimpleNamespace(mcp_app_loop=asyncio.get_running_loop(), external_tasks=tasks)
    )
    token = context.set_app(app)
    try:
        await executor.call_tool_result("fs_list_directory", {"path": "/tmp"})
    finally:
        context.reset(token)
    assert built == [fs_server]
    assert [(server, tool) for server, _, tool in tasks.calls] == [("fs", "list_directory")]


@pytest.mark.asyncio
async def test_an_unknown_name_is_not_given_a_builtin_route() -> None:
    gateway = FastMCP("gw")
    tasks = _RecordingTasks()
    built: list[Any] = []

    def factory(server: Any) -> Any:
        built.append(server)
        return SimpleNamespace(transport=object(), server=server)

    executor = AsyncMCPToolExecutor(gateway, client_factory=factory, preloaded_tools={})
    executor._mcp_tools = {}
    app = SimpleNamespace(
        state=SimpleNamespace(mcp_app_loop=asyncio.get_running_loop(), external_tasks=tasks)
    )
    token = context.set_app(app)
    try:
        await executor.call_tool_result("fs_invented", {})
    finally:
        context.reset(token)
    assert built == [gateway]
