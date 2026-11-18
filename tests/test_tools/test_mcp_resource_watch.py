"""Watching one resource for updates (host semantics HS3).

The 2026-07-28 protocol watches a resource through ``subscriptions/listen``, and the
SDK drops a bare ``notifications/resources/updated`` on such a connection. A server on
an older protocol pushes it unsolicited instead. Both reach
:mod:`clio_agent.tools.mcp_resource_updates`.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from anyio import BrokenResourceError, ClosedResourceError
from mcp.client import subscriptions as subscriptions_module
from mcp.client.subscriptions import ListenNotSupportedError, SubscriptionLost
from mcp.shared.exceptions import MCPError
from mcp.shared.subscriptions import ResourceUpdated
from mcp_types.jsonrpc import CONNECTION_CLOSED

from clio_agent.gact.a2ui import apply_batch
from clio_agent.gact.a2ui_catalogs.builtin import basic_catalog_id
from clio_agent.gact.a2ui_catalogs.registry import CatalogRegistry
from clio_agent.gact.server_surfaces import a2ui_resources, payload_messages, result_blocks
from clio_agent.tools import mcp_resource_updates
from clio_agent.tools.mcp_listen import list_changed_message_handler, watch_resource
from clio_agent.tools.mcp_namespace_executor import AsyncNamespacePreparationMixin

URI = "a2ui://fixture/panel"
_SERVER = Path(__file__).resolve().parents[1] / "fixtures" / "plugins" / "host_semantics"


def _hostfx() -> Any:
    spec = importlib.util.spec_from_file_location("hostfx_server_tools", _SERVER / "server.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def updates() -> Any:
    seen: list[tuple[str, str]] = []
    remove = mcp_resource_updates.add_listener(lambda ns, uri: seen.append((ns, uri)))
    yield seen
    remove()


class _Stream:
    def __init__(self, items: list[Any]) -> None:
        self._items = items

    async def __aiter__(self) -> AsyncIterator[Any]:
        for item in self._items:
            if isinstance(item, BaseException):
                raise item
            yield item


def _scripted(monkeypatch: pytest.MonkeyPatch, scripts: list[Any]) -> list[list[str]]:
    opened: list[list[str]] = []

    @asynccontextmanager
    async def listen(_session: Any, **kwargs: Any) -> AsyncIterator[_Stream]:
        assert set(kwargs) == {"resource_subscriptions"}
        opened.append(list(kwargs["resource_subscriptions"]))
        script = scripts.pop(0)
        if isinstance(script, BaseException):
            raise script
        yield _Stream(script)

    monkeypatch.setattr(subscriptions_module, "listen", listen)
    return opened


def _connect() -> Any:
    async def connect() -> Any:
        return SimpleNamespace(session=object())

    return connect


@pytest.mark.parametrize(
    "refusal",
    [MCPError(-32601, "Method not found"), ListenNotSupportedError("2025-11-25")],
)
async def test_a_server_that_cannot_listen_ends_the_watch_quietly(
    monkeypatch: pytest.MonkeyPatch, updates: list[tuple[str, str]], refusal: Exception
) -> None:
    opened = _scripted(monkeypatch, [refusal])

    await watch_resource(_connect(), "fx", URI, retry_after=0)

    assert opened == [[URI]]
    assert updates == []


async def test_a_lost_stream_is_reopened_and_reports_one_catch_up_update(
    monkeypatch: pytest.MonkeyPatch, updates: list[tuple[str, str]]
) -> None:
    opened = _scripted(
        monkeypatch,
        [
            [ResourceUpdated(uri=URI), SubscriptionLost("dropped")],
            [ResourceUpdated(uri=f"{URI}/part")],
            MCPError(-32601, "Method not found"),
        ],
    )

    await watch_resource(_connect(), "fx", URI, retry_after=0)

    assert opened == [[URI], [URI], [URI]]
    # One live update, one catch-up on resume, one sub-resource update reported as URI.
    assert updates == [("fx", URI)] * 3


async def test_any_other_listen_rejection_is_raised(monkeypatch: pytest.MonkeyPatch) -> None:
    _scripted(monkeypatch, [MCPError(-32600, "Invalid request")])

    with pytest.raises(MCPError):
        await watch_resource(_connect(), "fx", URI, retry_after=0)


async def test_an_unsolicited_update_is_reported_for_its_namespace(
    updates: list[tuple[str, str]],
) -> None:
    from mcp_types import ResourceUpdatedNotification

    handler = list_changed_message_handler("fx")
    await handler(ResourceUpdatedNotification(params={"uri": URI}))

    assert updates == [("fx", URI)]


async def test_an_update_from_a_listen_stream_is_left_to_its_watch(
    updates: list[tuple[str, str]],
) -> None:
    from mcp.shared.subscriptions import SUBSCRIPTION_ID_META_KEY
    from mcp_types import ResourceUpdatedNotification, ToolListChangedNotification

    handler = list_changed_message_handler("fx")
    stamp = {"_meta": {SUBSCRIPTION_ID_META_KEY: 7}}
    await handler(ResourceUpdatedNotification(params={"uri": URI, **stamp}))
    await handler(ToolListChangedNotification(params=stamp))

    assert updates == []


async def test_one_change_is_reported_once_with_a_push_handler_and_a_watch(
    updates: list[tuple[str, str]],
) -> None:
    from fastmcp import Client

    hostfx = _hostfx()
    server = hostfx.build_server()
    handler = list_changed_message_handler("hostfx")
    async with Client(server, message_handler=handler) as client:

        async def connect() -> Any:
            return client

        watch = asyncio.create_task(
            watch_resource(connect, "hostfx", hostfx.PANEL_URI, retry_after=0.05)
        )
        await client.call_tool("show_panel", {})
        for _ in range(200):
            await client.call_tool("bump", {"by": 1})
            await asyncio.sleep(0.02)
            if updates:
                break
        await asyncio.sleep(0.2)
        updates.clear()
        await client.call_tool("bump", {"by": 1})
        await asyncio.sleep(0.3)
        watch.cancel()
        with pytest.raises(asyncio.CancelledError):
            await watch

    assert updates == [("hostfx", hostfx.PANEL_URI)]


async def test_the_fixture_server_pushes_its_panel_update(
    updates: list[tuple[str, str]],
) -> None:
    from fastmcp import Client

    hostfx = _hostfx()
    server = hostfx.build_server()
    async with Client(server) as client:

        async def connect() -> Any:
            return client

        watch = asyncio.create_task(
            watch_resource(connect, "hostfx", hostfx.PANEL_URI, retry_after=0.05)
        )
        shown = await client.call_tool("show_panel", {})
        read = await client.read_resource(hostfx.PANEL_URI)
        for _ in range(200):
            await client.call_tool("bump", {"by": 2})
            await asyncio.sleep(0.02)
            if updates:
                break
        watch.cancel()
        with pytest.raises(asyncio.CancelledError):
            await watch

    assert updates and set(updates) == {("hostfx", hostfx.PANEL_URI)}
    registry = CatalogRegistry()
    producible = frozenset({basic_catalog_id()})
    resources = a2ui_resources(result_blocks(shown))
    assert len(resources) == 1
    surfaces, _ = apply_batch(
        {}, "s", payload_messages(resources[0]), catalogs=registry, producible=producible
    )
    assert list(surfaces) == [("s", hostfx.SURFACE_ID)]
    contents = read if isinstance(read, list) else read.contents
    assert json.loads(contents[0].text)[0]["createSurface"]["catalogId"] == basic_catalog_id()


@pytest.mark.parametrize(
    "closed",
    [
        ClosedResourceError(),
        BrokenResourceError(),
        MCPError(CONNECTION_CLOSED, "Connection closed"),
    ],
)
async def test_a_closed_connection_ends_the_watch_after_its_catch_up(
    monkeypatch: pytest.MonkeyPatch, updates: list[tuple[str, str]], closed: Exception
) -> None:
    opened = _scripted(monkeypatch, [[closed]])

    await watch_resource(_connect(), "fx", URI, catch_up=True, retry_after=0)

    # A watch replacing an earlier connection's reports one update on acknowledgement;
    # the closed connection ends it without reconnecting (the next connection starts one).
    assert opened == [[URI]]
    assert updates == [("fx", URI)]


class _Namespaces(AsyncNamespacePreparationMixin):
    """The executor's namespace bookkeeping, with a connection per mounted server."""

    def __init__(self) -> None:
        self._client = object()
        self._closed = False
        self._mcp_tools: dict[str, Any] = {}
        self._namespace_clients: dict[str, Any] = {}
        self._namespace_ctxs: dict[str, Any] = {}
        self._namespace_direct_routes: dict[str, bool] = {}
        self._namespace_heal_attempted: set[str] = set()
        self._namespace_locks: dict[str, Any] = {}
        self._namespace_servers: dict[str, Any] = {"fx": "server-1"}
        self._connected_namespaces: set[str] = set()
        self._resource_watches: dict[tuple[str, str], asyncio.Task[None]] = {}
        self.connected: list[Any] = []

    async def _connect_namespace(self, namespace: str, proxy: Any) -> Any:
        client = SimpleNamespace(server=proxy)
        self.connected.append(proxy)
        self._namespace_clients[namespace] = client
        self._namespace_direct_routes[namespace] = True
        self._ensure_resource_watches(namespace)
        return client


async def test_a_remounted_namespace_watches_its_resource_on_the_new_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from clio_agent.tools import mcp_listen

    watched: list[tuple[str, str, bool, Any]] = []

    async def watch(connect: Any, namespace: str, uri: str, *, catch_up: bool = False) -> None:
        client = await connect()
        watched.append((namespace, uri, catch_up, client.server))
        await asyncio.Event().wait()

    monkeypatch.setattr(mcp_listen, "watch_resource", watch)
    executor = _Namespaces()

    assert await executor.set_resource_subscription("fx", URI, subscribed=True) is True
    assert await executor.set_resource_subscription("fx", URI, subscribed=True) is True
    await asyncio.sleep(0.01)
    first = executor._resource_watches[("fx", URI)]
    assert watched == [("fx", URI, False, "server-1")]

    # A remount: the namespace now routes to a new server and its connection is reset.
    executor._namespace_servers["fx"] = "server-2"
    await executor.reset_namespace("fx")
    await asyncio.sleep(0.01)

    assert first.cancelled()
    assert executor.connected == ["server-1", "server-2"]
    assert watched[1:] == [("fx", URI, True, "server-2")]
    assert not executor._resource_watches[("fx", URI)].done()

    assert await executor.set_resource_subscription("fx", URI, subscribed=False) is False
    assert executor._resource_watches == {}
    await executor.reset_namespace("fx")  # nothing wanted: no reconnect
    assert executor.connected == ["server-1", "server-2"]
    await executor._stop_resource_watches()
