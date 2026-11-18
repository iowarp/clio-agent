"""A connected server's tool-list changes arrive over ``subscriptions/listen``.

At 2026-07-28 a server tells a client its tools changed only on a listen stream the
client opened with ``tools_list_changed``. The executor holds one such stream per
connected namespace (:func:`clio_agent.tools.mcp_listen.watch_tools`), reopens it when
it is lost, and re-lists the namespace over its own connection when it fires.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

import mcp_types
import pytest
from anyio import ClosedResourceError
from fastmcp import FastMCP
from fastmcp.tools.base import Tool
from fastmcp_tasks.extension import TasksExtension
from mcp.client import subscriptions as subscriptions_module
from mcp.client.subscriptions import ListenNotSupportedError, SubscriptionLost
from mcp.server.subscriptions import InMemorySubscriptionBus, ListenHandler
from mcp.shared.exceptions import MCPError
from mcp.shared.subscriptions import ToolsListChanged

from clio_agent.tools import gateway, mcp_config, mcp_tools_changed
from clio_agent.tools.gateway import build_gateway, namespace_proxies
from clio_agent.tools.mcp_config import MCPServerSpec
from clio_agent.tools.mcp_executor import AsyncMCPToolExecutor
from clio_agent.tools.mcp_listen import watch_tools
from clio_agent.tools.mcp_namespace_executor import AsyncNamespacePreparationMixin


@pytest.fixture(autouse=True)
def _fresh_generations() -> Any:
    mcp_tools_changed.reset_for_tests()
    yield
    mcp_tools_changed.reset_for_tests()


class _Stream:
    def __init__(self, items: list[Any]) -> None:
        self._items = items

    async def __aiter__(self) -> AsyncIterator[Any]:
        for item in self._items:
            if isinstance(item, BaseException):
                raise item
            yield item


def _scripted(monkeypatch: pytest.MonkeyPatch, scripts: list[Any]) -> list[dict[str, Any]]:
    opened: list[dict[str, Any]] = []

    @asynccontextmanager
    async def listen(_session: Any, **kwargs: Any) -> AsyncIterator[_Stream]:
        opened.append(kwargs)
        script = scripts.pop(0)
        if isinstance(script, BaseException):
            raise script
        yield _Stream(script)

    monkeypatch.setattr(subscriptions_module, "listen", listen)
    return opened


def _session(client: Any = None) -> Any:
    @asynccontextmanager
    async def session() -> AsyncIterator[Any]:
        yield SimpleNamespace(session=object()) if client is None else client

    return session


@pytest.mark.parametrize(
    "refusal",
    [
        MCPError(-32601, "Method not found"),
        ListenNotSupportedError("2025-11-25"),
        MCPError(-32000, "Connection closed"),
        ClosedResourceError(),
    ],
)
async def test_a_server_that_cannot_listen_or_has_gone_ends_the_watch_quietly(
    monkeypatch: pytest.MonkeyPatch, refusal: Exception
) -> None:
    opened = _scripted(monkeypatch, [refusal])

    await watch_tools(_session(), "fx", retry_after=0)

    assert opened == [{"tools_list_changed": True}]
    assert mcp_tools_changed.tools_changed_generation("fx") == 0


async def test_a_lost_stream_is_reopened_and_records_one_catch_up_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opened = _scripted(
        monkeypatch,
        [
            [ToolsListChanged(), SubscriptionLost("dropped")],
            TimeoutError("no acknowledgement"),
            [ToolsListChanged()],
            MCPError(-32601, "Method not found"),
        ],
    )

    await watch_tools(_session(), "fx", retry_after=0)

    assert opened == [{"tools_list_changed": True}] * 4
    # One live change, one catch-up on resume, one more live change.
    assert mcp_tools_changed.tools_changed_generation("fx") == 3


async def test_a_replacement_watch_catches_up_on_its_first_acknowledgement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _scripted(monkeypatch, [[], [], MCPError(-32601, "Method not found")])

    await watch_tools(_session(), "fx", catch_up=True, retry_after=0)

    # The first acknowledgement catches up, then the reopened stream catches up again.
    assert mcp_tools_changed.tools_changed_generation("fx") == 2


async def test_no_connection_ends_the_watch_without_listening(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opened = _scripted(monkeypatch, [])

    @asynccontextmanager
    async def gone() -> AsyncIterator[None]:
        yield None

    await watch_tools(gone, "fx", retry_after=0)

    assert opened == []


async def test_any_other_listen_rejection_is_raised(monkeypatch: pytest.MonkeyPatch) -> None:
    _scripted(monkeypatch, [MCPError(-32600, "Invalid request")])

    with pytest.raises(MCPError):
        await watch_tools(_session(), "fx", retry_after=0)


def _listening_backend(name: str, *, tasks: bool) -> tuple[FastMCP, dict[str, Any]]:
    """A 2026-07-28 server that announces tool changes only on listen streams."""

    backend = FastMCP(name)
    if tasks:
        backend.add_extension(TasksExtension())
    bus = InMemorySubscriptionBus()
    backend._mcp_server.add_request_handler(
        "subscriptions/listen", mcp_types.SubscriptionsListenRequestParams, ListenHandler(bus)
    )
    state: dict[str, Any] = {"bus": bus}

    def add_code_cell(code: str) -> str:
        """Add a code cell."""
        return f"added {code}"

    @backend.tool
    async def hello() -> str:
        """Answer once the server is up."""
        return "hi"

    @backend.tool
    async def open_connection() -> bool:
        """Unlock ``add_code_cell`` and announce it on the listen streams."""
        if "add_code_cell" not in {tool.name for tool in await backend.list_tools()}:
            backend.add_tool(Tool.from_function(add_code_cell))
        await bus.publish(ToolsListChanged())
        return True

    @backend.tool
    async def disconnect() -> bool:
        """Drop ``add_code_cell`` and announce it on the listen streams."""
        if "add_code_cell" in {tool.name for tool in await backend.list_tools()}:
            backend.local_provider.remove_tool("add_code_cell")
        await bus.publish(ToolsListChanged())
        return True

    return backend, state


def _executor(
    monkeypatch: pytest.MonkeyPatch, namespace: str, backend: FastMCP
) -> AsyncMCPToolExecutor:
    monkeypatch.setattr(gateway, "transport_for", lambda spec, cwd=None: backend)
    # The direct route builds its client through the config module's transport_for.
    monkeypatch.setattr(mcp_config, "transport_for", lambda spec, cwd=None: backend)
    gw = build_gateway({namespace: MCPServerSpec(name=namespace, transport="stdio", command="x")})
    return AsyncMCPToolExecutor(gw, namespace_servers=namespace_proxies(gw))


async def _until(namespace: str, generation: int, executor: Any, tool: str) -> None:
    """Call ``tool`` until the listen stream carries its change (the stream opens async)."""

    for _ in range(200):
        await executor.call_tool(f"{namespace}_{tool}", {})
        await asyncio.sleep(0.02)
        if mcp_tools_changed.tools_changed_generation(namespace) >= generation:
            return
    raise AssertionError("the server's tools/list_changed never reached the executor")


@pytest.mark.parametrize("tasks", [False, True], ids=["proxied", "direct"])
async def test_listen_carries_added_and_removed_tools_into_the_table(
    monkeypatch: pytest.MonkeyPatch, tasks: bool
) -> None:
    namespace = "listenfx" + ("t" if tasks else "p")
    backend, _ = _listening_backend(namespace, tasks=tasks)
    async with _executor(monkeypatch, namespace, backend) as executor:
        if tasks:
            # A task server's first start connects directly and stays direct.
            await executor.list_namespace_tools(namespace)
        await executor.call_tool(f"{namespace}_hello", {})
        assert executor._namespace_direct_routes[namespace] is tasks

        await _until(namespace, 1, executor, "open_connection")
        refreshed = await executor.refresh_changed_namespaces()

        assert refreshed[namespace][0] == [f"{namespace}_add_code_cell"]
        assert f"{namespace}_add_code_cell" in executor.get_tool_names()

        await _until(
            namespace,
            mcp_tools_changed.tools_changed_generation(namespace) + 1,
            executor,
            "disconnect",
        )
        refreshed = await executor.refresh_changed_namespaces()

        assert refreshed[namespace][1] == [f"{namespace}_add_code_cell"]
        assert f"{namespace}_add_code_cell" not in executor.get_tool_names()
        watch = executor.__dict__["_clio_tools_watches"][namespace]
    assert watch.done()  # closing the executor stops its watches


async def test_without_the_watch_a_listen_only_change_never_arrives(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(AsyncNamespacePreparationMixin, "_ensure_tools_watch", lambda *_: None)
    namespace = "listenfxn"
    backend, _ = _listening_backend(namespace, tasks=False)
    async with _executor(monkeypatch, namespace, backend) as executor:
        for _ in range(10):
            await executor.call_tool(f"{namespace}_open_connection", {})
            await asyncio.sleep(0.02)

        assert mcp_tools_changed.tools_changed_generation(namespace) == 0
        assert executor.namespaces_pending_refresh() == {}


async def test_a_reconnected_namespace_gets_a_new_watch_that_catches_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = "listenfxr"
    backend, _ = _listening_backend(namespace, tasks=False)
    async with _executor(monkeypatch, namespace, backend) as executor:
        await _until(namespace, 1, executor, "open_connection")
        first = executor.__dict__["_clio_tools_watches"][namespace]
        before = mcp_tools_changed.tools_changed_generation(namespace)

        await executor.reset_namespace(namespace)
        assert first.done()
        await executor.call_tool(f"{namespace}_hello", {})
        second = executor.__dict__["_clio_tools_watches"][namespace]

        assert second is not first
        for _ in range(200):
            if mcp_tools_changed.tools_changed_generation(namespace) > before:
                break
            await asyncio.sleep(0.02)
        # The new stream's first acknowledgement records one catch-up change.
        assert mcp_tools_changed.tools_changed_generation(namespace) == before + 1
