"""A pooled MCP connection keeps nothing of the turn whose tool call opened it.

Live (#1846): the session task a client starts copied the opening turn's context, so
each pooled connection kept that turn's ReAct loop, module, tools and call ledger alive.
"""

from __future__ import annotations

import asyncio
import contextvars
import gc
import weakref
from collections.abc import Awaitable, Callable
from typing import Any

from fastmcp import Client, FastMCP

from clio_agent.tools import mcp_server_progress
from clio_agent.tools.mcp_runtime import enter_connection

_TURN_STATE: contextvars.ContextVar[object] = contextvars.ContextVar("test_turn_state")


class _Agent:
    """Stands in for what a turn's context carries."""


def _server() -> FastMCP:
    server = FastMCP("probe")

    @server.tool
    def ping() -> str:
        return "pong"

    return server


async def _turn(enter: Callable[[Any], Awaitable[Any]], ref: list[weakref.ref]) -> Any:
    agent = _Agent()
    ref.append(weakref.ref(agent))
    _TURN_STATE.set(agent)
    return await enter(Client(_server()))


async def _opened_in_a_turn(enter: Callable[[Any], Awaitable[Any]]) -> tuple[Any, weakref.ref]:
    ref: list[weakref.ref] = []
    # The turn runs in its own task (its own context), as a turn's tool call does.
    client = await asyncio.get_running_loop().create_task(_turn(enter, ref))
    await asyncio.sleep(0)
    gc.collect()
    return client, ref[0]


async def _plain_enter(client_ctx: Any) -> Any:
    return await client_ctx.__aenter__()


def test_a_connection_does_not_keep_the_opening_turn_alive() -> None:
    async def scenario() -> None:
        client, ref = await _opened_in_a_turn(enter_connection)
        try:
            assert ref() is None
            assert (await client.call_tool("ping", {})).data == "pong"
        finally:
            await client.__aexit__(None, None, None)

    asyncio.run(scenario())


def test_entering_in_the_turn_kept_it_alive() -> None:
    """The failure mode the helper removes, so the test above means something."""

    async def scenario() -> None:
        client, ref = await _opened_in_a_turn(_plain_enter)
        try:
            assert ref() is not None
        finally:
            await client.__aexit__(None, None, None)

    asyncio.run(scenario())


def test_the_connection_context_keeps_only_the_waits_server_tree() -> None:
    _TURN_STATE.set(_Agent())
    tree = object()
    token = mcp_server_progress._SERVER_TREE.set(tree)  # type: ignore[arg-type]
    try:
        context = mcp_server_progress.connection_context()
    finally:
        mcp_server_progress._SERVER_TREE.reset(token)

    assert dict(context) == {mcp_server_progress._SERVER_TREE: tree}
