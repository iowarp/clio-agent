"""``asyncio.wait`` lets go of its caller once it returns.

Live (#1846): FastMCP waits on each tool call together with the client's long-lived
session task, and Python 3.14's ``asyncio.wait`` left every calling task (and the turn
context it ran in) recorded on that session task for good.
"""

from __future__ import annotations

import asyncio
import contextvars
import gc
import weakref

from fastmcp import Client, FastMCP

from clio_agent.runtime import asyncio_wait_release
from clio_agent.tools.mcp_runtime import enter_connection

_TURN_STATE: contextvars.ContextVar[object] = contextvars.ContextVar("test_wait_turn_state")


class _Agent:
    """Stands in for what a turn's context carries."""


async def _waits_beside(long_lived: asyncio.Future[None], refs: list[weakref.ref]) -> None:
    agent = _Agent()
    refs.append(weakref.ref(agent))
    _TURN_STATE.set(agent)
    quick = asyncio.ensure_future(asyncio.sleep(0))
    await asyncio.wait({quick, long_lived}, return_when=asyncio.FIRST_COMPLETED)


def test_a_pending_future_keeps_none_of_its_waiters() -> None:
    asyncio_wait_release.install()

    async def scenario() -> None:
        loop = asyncio.get_running_loop()
        long_lived: asyncio.Future[None] = loop.create_future()
        refs: list[weakref.ref] = []
        for _ in range(5):
            await loop.create_task(_waits_beside(long_lived, refs))
        await _settle()
        assert not getattr(long_lived, "_asyncio_awaited_by", None)
        assert sum(1 for ref in refs if ref() is not None) <= 1
        long_lived.cancel()

    asyncio.run(scenario())


def test_install_is_idempotent() -> None:
    asyncio_wait_release.install()
    wrapped = asyncio.tasks._wait  # type: ignore[attr-defined]
    asyncio_wait_release.install()
    assert asyncio.tasks._wait is wrapped  # type: ignore[attr-defined]


async def _settle() -> None:
    await asyncio.sleep(0)
    gc.collect()


async def _call_in_a_turn(client: Client, refs: list[weakref.ref]) -> None:
    agent = _Agent()
    refs.append(weakref.ref(agent))
    _TURN_STATE.set(agent)
    assert (await client.call_tool("ping", {})).data == "pong"


def test_mcp_calls_on_a_pooled_session_keep_no_turn_alive() -> None:
    server = FastMCP("probe")

    @server.tool
    def ping() -> str:
        return "pong"

    async def scenario() -> None:
        client = await enter_connection(Client(server))
        refs: list[weakref.ref] = []
        try:
            loop = asyncio.get_running_loop()
            for _ in range(6):
                await loop.create_task(_call_in_a_turn(client, refs))
            await _settle()
            session = client._session_state.session_task
            assert not getattr(session, "_asyncio_awaited_by", None)
            # At most the newest call's task, still referenced by this frame.
            assert sum(1 for ref in refs if ref() is not None) <= 1
        finally:
            await client.__aexit__(None, None, None)

    asyncio.run(scenario())
