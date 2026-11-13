"""A sync MCP executor in a GACT server runs its server sessions on the server loop.

Product calls run on the application loop through a namespace's proxy, whose kept-alive
backend session is shared with the executor's own connections. An executor built where
no app is bound (task recovery after a restart, the commands listing) used to own a
private loop: the executor connected the server there, and a call from the application
loop then wrote to a session whose reader ran on the other loop. The request never
reached the server and the call waited forever (seen live: a view's reopen after a
restart, and an open in flight while its connection was saved again).
"""

from __future__ import annotations

import asyncio
import sys
import threading
from collections.abc import Iterator
from pathlib import Path

import psutil
import pytest

from clio_agent.arc import loop_guard
from clio_agent.tools import task_call_context

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "mcp_apps" / "channel_app_server.py"


@pytest.fixture
def server_loop() -> Iterator[asyncio.AbstractEventLoop]:
    """A running, registered server loop on its own thread, as the app's lifespan makes."""

    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, name="test-server-loop", daemon=True)
    thread.start()
    loop_guard.register_server_loop(loop)
    try:
        yield loop
    finally:
        loop_guard.unregister_server_loop(loop)
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=5)
        loop.close()


def test_outside_a_server_an_executor_owns_its_loop() -> None:
    assert task_call_context.executor_loop() is None


def test_a_builder_with_no_app_bound_gets_the_server_loop(
    server_loop: asyncio.AbstractEventLoop,
) -> None:
    assert task_call_context.executor_loop() is server_loop


def test_on_the_server_loop_thread_it_keeps_a_private_loop_with_a_reason(
    server_loop: asyncio.AbstractEventLoop, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[str] = []
    from clio_agent.runtime import trace

    monkeypatch.setattr(trace, "event", lambda _kind, fmt, *args: events.append(fmt % args))

    async def resolve() -> object:
        return task_call_context.executor_loop()

    assert asyncio.run_coroutine_threadsafe(resolve(), server_loop).result(5) is None
    assert events == [
        f"{task_call_context.MCP_EXECUTOR_PRIVATE_LOOP} reason=built_on_server_loop_thread"
    ]


def test_several_server_loops_are_not_guessed_between(
    server_loop: asyncio.AbstractEventLoop, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[str] = []
    from clio_agent.runtime import trace

    monkeypatch.setattr(trace, "event", lambda _kind, fmt, *args: events.append(fmt % args))
    other = asyncio.new_event_loop()
    thread = threading.Thread(target=other.run_forever, daemon=True)
    thread.start()
    loop_guard.register_server_loop(other)
    try:
        assert task_call_context.executor_loop() is None
    finally:
        loop_guard.unregister_server_loop(other)
        other.call_soon_threadsafe(other.stop)
        thread.join(timeout=5)
        other.close()
    assert events == [f"{task_call_context.MCP_EXECUTOR_PRIVATE_LOOP} reason=several_server_loops"]


@pytest.mark.timeout(90)  # starts a real stdio server twice over (list, then call)
def test_a_call_on_the_server_loop_reaches_a_server_the_executor_connected(
    server_loop: asyncio.AbstractEventLoop, tmp_path: Path
) -> None:
    from fastmcp import Client

    from clio_agent.tools.execution import create_sync_tool_executor
    from clio_agent.tools.gateway import build_gateway, namespace_proxies
    from clio_agent.tools.mcp_config import MCPServerSpec
    from clio_agent.tools.mcp_runtime import make_mcp_client

    log = tmp_path / "calls.jsonl"
    spec = MCPServerSpec(
        name="timeline",
        transport="stdio",
        command=sys.executable,
        args=(str(FIXTURE),),
        env={"CHANNEL_APP_LOG": str(log)},
    )
    gateway = build_gateway({"timeline": spec}, cwd=str(tmp_path))
    # Built with no app bound, as task recovery and the commands listing build a fleet.
    executor = create_sync_tool_executor(
        gateway,
        preloaded_tools={},
        namespace_servers=namespace_proxies(gateway),
        client_factory=make_mcp_client,
    )
    try:
        listed = executor.list_namespace_tools("timeline")  # its first start, direct
        assert {tool.name for tool in listed} >= {"open_timeline", "set_time"}
        # The commands listing reads prompts over the namespace's persistent client,
        # which for a plain server is the proxy's kept-alive backend session.
        executor.list_namespace_prompts("timeline")

        async def product_call() -> str:
            # A product call: its own connection to the proxy, on the server loop.
            async with make_mcp_client(namespace_proxies(gateway)["timeline"]) as client:
                assert isinstance(client, Client)
                result = await client.call_tool("set_time", {"time": 7})
                return result.content[0].text

        answer = asyncio.run_coroutine_threadsafe(
            asyncio.wait_for(product_call(), 30), server_loop
        ).result(40)
        assert answer == "Timeline moved to step 7."
        assert '"set_time"' in log.read_text(encoding="utf-8")
        assert executor._loop is server_loop  # noqa: SLF001
    finally:
        executor.close()
        for process in psutil.Process().children(recursive=True):
            try:
                if any(FIXTURE.name in arg for arg in process.cmdline()):
                    process.kill()
            except psutil.Error:
                continue
