"""An executor's direct namespace holds the application's kept-alive connection."""

from __future__ import annotations

import asyncio
import hashlib
import json
from types import SimpleNamespace
from typing import Any

import pytest
from fastmcp import FastMCP

from clio_agent.tools import mcp_executor, shared_direct_connections
from clio_agent.tools.mcp_executor import AsyncMCPToolExecutor
from clio_agent.tools.mcp_task_routing import NamespaceRouteDecision


def _executor(monkeypatch: pytest.MonkeyPatch, direct: Any) -> AsyncMCPToolExecutor:
    executor = AsyncMCPToolExecutor(
        FastMCP("gw"), client_factory=lambda server: server, preloaded_tools={}
    )
    executor._clio_namespace_specs = {"ns": {"command": "srv", "args": ["serve"]}}  # type: ignore[attr-defined]
    executor._clio_namespace_direct_factories = {"ns": lambda: direct}  # type: ignore[attr-defined]
    decision = NamespaceRouteDecision(use_direct=direct is not None, reason=None)
    monkeypatch.setattr(
        mcp_executor, "resolve_and_build_direct_client", lambda *_: (direct, decision)
    )
    return executor


@pytest.mark.asyncio
async def test_a_direct_namespace_holds_the_shared_connection_and_releases_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SABOTAGE: drop the ``hold`` branch of ``_enter_namespace`` and the executor
    starts its own server process (``enter_connection`` on the fresh client)."""
    fresh, shared, proxy = object(), SimpleNamespace(name="shared"), object()
    held: list[tuple[str, str, Any, Any]] = []
    released: list[bool] = []

    async def hold(server: str, scope: str, owner: Any, configured: Any) -> Any:
        held.append((server, scope, owner, configured))

        async def release() -> None:
            released.append(True)

        return shared, release

    async def entered(_ctx: Any) -> Any:
        raise AssertionError("the executor must not start its own connection")

    monkeypatch.setattr(mcp_executor, "enter_connection", entered)
    executor = _executor(monkeypatch, fresh)
    remove = shared_direct_connections.register(asyncio.get_running_loop(), hold, lambda: True)
    try:
        assert await executor._connect_namespace("ns", proxy) is shared
    finally:
        remove()
    scope = hashlib.sha256(
        json.dumps(executor._clio_namespace_specs["ns"], sort_keys=True).encode()  # type: ignore[attr-defined]
    ).hexdigest()
    assert held == [("ns", scope, proxy, fresh)]
    assert executor._namespace_direct_routes["ns"] is True

    await executor._evict_namespace_client("ns")
    await executor._evict_namespace_client("ns")
    assert released == [True]


@pytest.mark.asyncio
async def test_without_a_holder_or_a_direct_route_the_executor_connects_itself(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered: list[Any] = []

    async def enter(ctx: Any) -> Any:
        entered.append(ctx)
        return ctx

    async def hold(*_: Any) -> Any:
        raise AssertionError("a proxied namespace never holds a direct connection")

    monkeypatch.setattr(mcp_executor, "enter_connection", enter)
    fresh = object()
    executor = _executor(monkeypatch, fresh)
    assert await executor._connect_namespace("ns", object()) is fresh  # no holder registered

    proxied = _executor(monkeypatch, None)
    proxy = object()
    remove = shared_direct_connections.register(asyncio.get_running_loop(), hold, lambda: True)
    try:
        assert await proxied._connect_namespace("ns", proxy) is proxy
    finally:
        remove()
    assert entered == [fresh, proxy]
