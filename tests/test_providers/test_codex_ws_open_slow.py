"""A slow Codex WebSocket open is retried longer and reported slow (#1577 3.10).

The 15 s ``open_timeout`` turned a slow handshake into the same retryable error as a dead
endpoint, and the keepalive settings were websockets' implicit defaults. Driven against a
REAL local WebSocket server (its handshake delayed) with the bounds shortened.
"""

from __future__ import annotations

import asyncio
import socket
import threading
from collections.abc import Iterator
from typing import Any

import pytest
import websockets
from dspy.lm15 import TimeoutError as RequestTimeoutError
from websockets.asyncio.server import serve

from clio_agent.providers.codex import constants as c
from clio_agent.providers.codex import direct_engine


@pytest.fixture
def delayed_ws_server() -> Iterator[tuple[dict[str, float], str]]:
    """A WebSocket server whose opening handshake answers after ``knobs['delay']`` s."""
    knobs = {"delay": 0.0}
    ready = threading.Event()
    stop: dict[str, Any] = {}
    port: dict[str, int] = {}

    async def _process_request(_connection: Any, _request: Any) -> None:
        await asyncio.sleep(knobs["delay"])
        return None

    async def _handler(ws: Any) -> None:
        await ws.wait_closed()

    async def _main() -> None:
        async with serve(_handler, "127.0.0.1", 0, process_request=_process_request) as server:
            port["n"] = next(iter(server.sockets)).getsockname()[1]
            stop["event"] = asyncio.Event()
            stop["loop"] = asyncio.get_running_loop()
            ready.set()
            await stop["event"].wait()

    thread = threading.Thread(target=asyncio.run, args=(_main(),), daemon=True)
    thread.start()
    assert ready.wait(10)
    try:
        yield knobs, f"ws://127.0.0.1:{port['n']}"
    finally:
        stop["loop"].call_soon_threadsafe(stop["event"].set)
        thread.join(timeout=10)


def test_a_slow_open_is_retried_with_a_longer_bound_and_keepalive_is_explicit(
    delayed_ws_server: tuple[dict[str, float], str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The handshake answers after 0.6 s; first bound 0.3 s, retry 3 s: connected.

    SABOTAGE: drop the longer retry -> RequestTimeoutError -> red.
    """
    knobs, url = delayed_ws_server
    knobs["delay"] = 0.6
    monkeypatch.setattr(c, "CODEX_WS_URL", url)
    monkeypatch.setattr(c, "WS_CONNECT_TIMEOUT_S", 0.3)
    monkeypatch.setattr(c, "WS_CONNECT_RETRY_TIMEOUT_S", 3.0)

    async def _open() -> tuple[Any, Any]:
        ws = await direct_engine._connect({"Authorization": "Bearer t"}, None)
        try:
            return ws.ping_interval, ws.ping_timeout
        finally:
            await ws.close()

    interval, timeout = asyncio.run(_open())
    assert (interval, timeout) == (c.WS_PING_INTERVAL_S, c.WS_PING_TIMEOUT_S)


def test_an_open_that_never_answers_is_typed_slow(monkeypatch: pytest.MonkeyPatch) -> None:
    """A listener that accepts and never answers: a typed, retryable "slow" timeout.

    SABOTAGE: word it as before ("did not answer ... within 15 s") without the retry ->
    the message check fails -> red.
    """
    silent = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    silent.bind(("127.0.0.1", 0))
    silent.listen(4)
    try:
        monkeypatch.setattr(c, "CODEX_WS_URL", f"ws://127.0.0.1:{silent.getsockname()[1]}")
        monkeypatch.setattr(c, "WS_CONNECT_TIMEOUT_S", 0.2)
        monkeypatch.setattr(c, "WS_CONNECT_RETRY_TIMEOUT_S", 0.4)
        with pytest.raises(RequestTimeoutError) as caught:
            asyncio.run(direct_engine._connect({"Authorization": "Bearer t"}, None))
        assert "slow or unresponsive" in str(caught.value)
    finally:
        silent.close()


def test_keepalive_constants_are_explicit() -> None:
    assert c.WS_PING_INTERVAL_S > 0 and c.WS_PING_TIMEOUT_S >= c.WS_PING_INTERVAL_S
    assert websockets  # the real library, not a stand-in
