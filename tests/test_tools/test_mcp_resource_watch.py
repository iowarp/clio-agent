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
from mcp.client import subscriptions as subscriptions_module
from mcp.client.subscriptions import ListenNotSupportedError, SubscriptionLost
from mcp.shared.exceptions import MCPError
from mcp.shared.subscriptions import ResourceUpdated

from clio_agent.gact.a2ui import apply_batch
from clio_agent.gact.a2ui_catalogs.builtin import basic_catalog_id
from clio_agent.gact.a2ui_catalogs.registry import CatalogRegistry
from clio_agent.gact.server_surfaces import a2ui_resources, payload_messages, result_blocks
from clio_agent.tools import mcp_resource_updates
from clio_agent.tools.mcp_listen import list_changed_message_handler, watch_resource

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
