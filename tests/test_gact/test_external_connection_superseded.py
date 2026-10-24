"""A retained external-MCP connection follows the current workspace fleet's proxy.

``ExternalTasks.connection`` keeps one connection per in-process server. Before this
was keyed on the server object, a fleet reaped for idleness left its connection
behind, and the next call went through the reaped proxy: its transport respawned the
server outside the fleet's (closed) network channel and the call failed with
``Connection closed`` (seen live with googlecolab/colab-mcp). In-process fakes only.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from fastmcp import Client, FastMCP

from clio_agent.gact import external_task_connections as connections


def _proxy(name: str) -> FastMCP:
    server = FastMCP(name)

    @server.tool
    def ping() -> str:
        return name

    return server


def _tasks() -> Any:
    return SimpleNamespace(_closing=False, connections={}, _opening={})


def test_the_owner_names_the_in_process_server_object() -> None:
    first, second = _proxy("a"), _proxy("b")

    assert connections._in_process_owner(Client(first)) == connections._in_process_owner(
        Client(first)
    )
    assert connections._in_process_owner(Client(first)) != connections._in_process_owner(
        Client(second)
    )
    assert connections._in_process_owner(Client("http://127.0.0.1:9/mcp")) == ""


@pytest.mark.asyncio
async def test_a_rebuilt_proxy_gets_its_own_connection_and_the_reaped_one_retires(
    caplog: pytest.LogCaptureFixture,
) -> None:
    tasks = _tasks()
    reaped, rebuilt = _proxy("reaped"), _proxy("rebuilt")

    first = await connections.connection(tasks, "colab-mcp", Client(reaped), "scope")
    assert await connections.connection(tasks, "colab-mcp", Client(reaped), "scope") is first

    with caplog.at_level("WARNING"):
        second = await connections.connection(tasks, "colab-mcp", Client(rebuilt), "scope")

    assert second is not first
    assert list(tasks.connections.values()) == [second]
    assert first.client is None  # the idle superseded connection was closed
    assert connections.EXTERNAL_MCP_CONNECTION_SUPERSEDED in caplog.text
    assert second.client is not None
    assert [t.name for t in await second.client.list_tools()] == ["ping"]
    await connections.retire_connection(tasks, second, shutdown=True)


@pytest.mark.asyncio
async def test_a_busy_superseded_connection_is_kept_until_its_work_ends(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    retired: list[Any] = []

    async def _retire(self: Any, host: Any, *, shutdown: bool = False) -> None:
        retired.append(host)

    monkeypatch.setattr(connections, "retire_connection", _retire)
    busy = SimpleNamespace(
        cache_key=("colab-mcp", "scope", "in-process:1"), rpcs={object()}, calls={}, drivers={}
    )
    idle = SimpleNamespace(
        cache_key=("colab-mcp", "scope", "in-process:2"), rpcs=set(), calls={}, drivers={}
    )
    other = SimpleNamespace(
        cache_key=("web", "scope", "in-process:3"), rpcs=set(), calls={}, drivers={}
    )
    remote = SimpleNamespace(cache_key=("colab-mcp", "scope", ""), rpcs=set(), calls={}, drivers={})
    current = SimpleNamespace(cache_key=("colab-mcp", "scope", "in-process:4"))
    tasks = SimpleNamespace(connections={h.cache_key: h for h in (busy, idle, other, remote)})

    await connections._retire_superseded(tasks, current)

    assert retired == [idle]
