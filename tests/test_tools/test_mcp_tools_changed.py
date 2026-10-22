"""A server's ``tools/list_changed`` reaches the executor's tool table (colab-mcp shape).

googlecolab/colab-mcp starts with one tool and unlocks the notebook tools on the SAME
process once a browser connects back, then sends ``notifications/tools/list_changed``.
These tests drive that shape in-process: a backend whose tool list changes on a call,
mounted through the real gateway proxy (``build_gateway``), behind the real
``AsyncMCPToolExecutor``. No subprocess, no network.
"""

from __future__ import annotations

import asyncio
from typing import Any

import mcp_types
import pytest
from fastmcp import Context, FastMCP
from fastmcp.tools.base import Tool

from clio_agent.tools import gateway, mcp_tools_changed
from clio_agent.tools.gateway import build_gateway, namespace_proxies
from clio_agent.tools.mcp_config import MCPServerSpec
from clio_agent.tools.mcp_executor import AsyncMCPToolExecutor

NAMESPACE = "colabish"


@pytest.fixture(autouse=True)
def _fresh_generations() -> Any:
    mcp_tools_changed.reset_for_tests()
    yield
    mcp_tools_changed.reset_for_tests()


def _unlocking_backend() -> FastMCP:
    """One starting tool that unlocks ``add_code_cell``; ``disconnect`` removes it again."""

    backend = FastMCP("colabish-backend")

    def add_code_cell(code: str) -> str:
        """Add a code cell."""
        return f"added {code}"

    async def _announce(ctx: Context) -> None:
        await ctx.send_notification(mcp_types.ToolListChangedNotification())

    @backend.tool
    async def open_connection(ctx: Context) -> bool:
        """Connect the notebook and unlock its tools."""
        backend.add_tool(Tool.from_function(add_code_cell))
        await _announce(ctx)
        return True

    @backend.tool
    async def disconnect(ctx: Context) -> bool:
        """Drop the notebook tools."""
        backend.local_provider.remove_tool("add_code_cell")
        await _announce(ctx)
        return True

    return backend


def _executor(monkeypatch: pytest.MonkeyPatch, backend: FastMCP) -> AsyncMCPToolExecutor:
    monkeypatch.setattr(gateway, "transport_for", lambda spec, cwd=None: backend)
    gw = build_gateway({NAMESPACE: MCPServerSpec(name=NAMESPACE, transport="stdio", command="x")})
    return AsyncMCPToolExecutor(gw, namespace_servers=namespace_proxies(gw))


async def _until_changed(expected: int) -> None:
    for _ in range(100):
        if mcp_tools_changed.tools_changed_generation(NAMESPACE) >= expected:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("the backend's tools/list_changed never reached the proxy client")


@pytest.mark.asyncio
async def test_unlocked_tools_are_relisted_over_the_connection_and_callable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with _executor(monkeypatch, _unlocking_backend()) as executor:
        assert f"{NAMESPACE}_add_code_cell" not in executor.get_tool_names()
        assert executor.namespaces_pending_refresh() == {}

        assert "true" in (await executor.call_tool(f"{NAMESPACE}_open_connection", {})).lower()
        await _until_changed(1)
        assert executor.namespaces_pending_refresh() == {NAMESPACE: 1}

        refreshed = await executor.refresh_changed_namespaces()

        assert refreshed == {NAMESPACE: ([f"{NAMESPACE}_add_code_cell"], [])}
        assert executor.tools_revision == 1
        assert f"{NAMESPACE}_add_code_cell" in executor.get_tool_names()
        # Same prefixed form a mount produces: the definition carries the full name.
        assert (
            executor.get_tool_definitions()[f"{NAMESPACE}_add_code_cell"].name
            == f"{NAMESPACE}_add_code_cell"
        )
        assert "added x" in await executor.call_tool(f"{NAMESPACE}_add_code_cell", {"code": "x"})
        # Seen: a second refresh is a no-op.
        assert await executor.refresh_changed_namespaces() == {}
        assert executor.tools_revision == 1


@pytest.mark.asyncio
async def test_a_removed_tool_disappears_from_the_table(monkeypatch: pytest.MonkeyPatch) -> None:
    async with _executor(monkeypatch, _unlocking_backend()) as executor:
        await executor.call_tool(f"{NAMESPACE}_open_connection", {})
        await _until_changed(1)
        await executor.refresh_changed_namespaces()

        await executor.call_tool(f"{NAMESPACE}_disconnect", {})
        await _until_changed(2)
        refreshed = await executor.refresh_changed_namespaces()

        assert refreshed == {NAMESPACE: ([], [f"{NAMESPACE}_add_code_cell"])}
        assert f"{NAMESPACE}_add_code_cell" not in executor.get_tool_names()
        assert f"{NAMESPACE}_open_connection" in executor.get_tool_names()
        assert executor.tools_revision == 2


@pytest.mark.asyncio
async def test_a_namespace_never_connected_is_skipped_typed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    monkeypatch.setattr(
        "clio_agent.runtime.trace.event", lambda _tag, fmt, *args: events.append(fmt % args)
    )
    async with _executor(monkeypatch, _unlocking_backend()) as executor:
        mcp_tools_changed.mark_tools_changed(NAMESPACE)  # another executor's process

        assert await executor.refresh_changed_namespaces() == {}

        assert f"{NAMESPACE}_add_code_cell" not in executor.get_tool_names()
        assert not executor.is_namespace_prepared(NAMESPACE)  # nothing was spawned for it
        assert any(e.startswith(mcp_tools_changed.MCP_TOOLS_REFRESH_NOT_CONNECTED) for e in events)
        assert executor.namespaces_pending_refresh() == {}


@pytest.mark.asyncio
async def test_a_failed_relist_keeps_the_old_definitions_typed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    async with _executor(monkeypatch, _unlocking_backend()) as executor:
        await executor.call_tool(f"{NAMESPACE}_open_connection", {})
        await _until_changed(1)
        before = executor.get_tool_names()

        async def _broken(namespace: str, proxy: Any) -> Any:
            raise ConnectionError("server gone")

        monkeypatch.setattr(executor, "_namespace_client", _broken)
        monkeypatch.setattr(
            "clio_agent.runtime.trace.event", lambda _tag, fmt, *args: events.append(fmt % args)
        )

        assert await executor.refresh_changed_namespaces() == {}

        assert executor.get_tool_names() == before
        assert executor.tools_revision == 0
        assert any(e.startswith(mcp_tools_changed.MCP_TOOLS_REFRESH_FAILED) for e in events)


def test_replace_namespace_entries_touches_only_that_namespace() -> None:
    table = {"a_x": 1, "a_y": 2, "b_x": 3}

    new, added, removed = mcp_tools_changed.replace_namespace_entries(
        table, "a", {"a_x": 10, "a_z": 30}
    )

    assert new == {"b_x": 3, "a_x": 10, "a_z": 30}
    assert (added, removed) == (["a_z"], ["a_y"])
    assert table == {"a_x": 1, "a_y": 2, "b_x": 3}  # the old table is left intact


def test_generations_count_per_namespace() -> None:
    assert mcp_tools_changed.mark_tools_changed("n1") == 1
    assert mcp_tools_changed.mark_tools_changed("n1") == 2
    assert mcp_tools_changed.mark_tools_changed("") == 0
    assert mcp_tools_changed.changed_since({"n1": 1}, ["n1", "n2"]) == {"n1": 2}
    assert mcp_tools_changed.changed_since({"n1": 2}, ["n1", "n2"]) == {}
