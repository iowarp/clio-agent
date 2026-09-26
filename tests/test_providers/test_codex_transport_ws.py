"""Integration tests for the WebSocket transport against a local mock server (A.6, A.9).

Covers connection reuse across turns with delta requests, the
``previous_response_not_found`` recovery, a pre-stream connection failure
(marks the session SSE-only), and cancel dropping the socket.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from typing import Any

import pytest
import websockets

from clio_agent.providers.codex import constants as c
from clio_agent.providers.codex.login_flow import CodexCredential
from clio_agent.providers.codex.sessions import CodexSessionState
from clio_agent.providers.codex.stream_events import Completed
from clio_agent.providers.codex.transport_ws import (
    WsPreStreamFailure,
    drop_connection_after_cancel,
    stream_ws_turn,
)

_CREDENTIAL = CodexCredential(
    access_token="at", refresh_token="rt", expires_at_ms=0, account_id="acct_1"
)


def _completed_frame(response_id: str, output: list[dict] | None = None) -> str:
    return json.dumps(
        {
            "type": "response.completed",
            "response": {
                "id": response_id,
                "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
                "output": output or [],
            },
        }
    )


@pytest.fixture
async def ws_server_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[Callable]:
    servers: list[Any] = []

    async def _start(handler: Callable) -> str:
        server = await websockets.serve(handler, "127.0.0.1", 0)
        servers.append(server)
        port = server.sockets[0].getsockname()[1]
        url = f"ws://127.0.0.1:{port}"
        monkeypatch.setattr(c, "CODEX_WS_URL", url)
        return url

    yield _start
    for server in servers:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_ws_reuse_across_three_turns_with_delta_on_2_and_3(
    ws_server_factory: Callable,
) -> None:
    received: list[dict] = []
    connection_count = {"n": 0}

    async def handler(websocket: Any) -> None:
        connection_count["n"] += 1
        async for raw in websocket:
            frame = json.loads(raw)
            received.append(frame)
            await websocket.send(
                _completed_frame(
                    f"resp_{len(received)}",
                    output=[
                        {
                            "type": "message",
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": "ok"}],
                        }
                    ],
                )
            )

    await ws_server_factory(handler)
    state = CodexSessionState(session_id="s1")

    turn1_input = [
        {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]}
    ]
    body1 = {"model": "m", "instructions": "x", "input": turn1_input}
    events1 = [
        e
        async for e in stream_ws_turn(
            credential=_CREDENTIAL, body=body1, session_id="s1", state=state
        )
    ]
    assert any(isinstance(e, Completed) for e in events1)
    assert state.counters.connections_created == 1
    assert state.counters.full_requests == 1

    turn1_output = [
        {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "ok"}]}
    ]
    new_turn2 = {
        "type": "message",
        "role": "user",
        "content": [{"type": "input_text", "text": "again"}],
    }
    body2 = {"model": "m", "instructions": "x", "input": [*turn1_input, *turn1_output, new_turn2]}
    events2 = [
        e
        async for e in stream_ws_turn(
            credential=_CREDENTIAL, body=body2, session_id="s1", state=state
        )
    ]
    assert any(isinstance(e, Completed) for e in events2)
    assert state.counters.connections_reused == 1
    assert state.counters.delta_requests == 1
    assert received[1]["input"] == [new_turn2]
    assert received[1]["previous_response_id"] == "resp_1"

    turn2_output = turn1_output
    new_turn3 = {
        "type": "message",
        "role": "user",
        "content": [{"type": "input_text", "text": "once more"}],
    }
    body3 = {
        "model": "m",
        "instructions": "x",
        "input": [*turn1_input, *turn1_output, new_turn2, *turn2_output, new_turn3],
    }
    events3 = [
        e
        async for e in stream_ws_turn(
            credential=_CREDENTIAL, body=body3, session_id="s1", state=state
        )
    ]
    assert any(isinstance(e, Completed) for e in events3)
    assert state.counters.connections_reused == 2
    assert state.counters.delta_requests == 2
    assert received[2]["input"] == [new_turn3]
    assert received[2]["previous_response_id"] == "resp_2"

    # One real socket serving all three turns.
    assert connection_count["n"] == 1
    await drop_connection_after_cancel(state)


@pytest.mark.asyncio
async def test_previous_response_not_found_recovers_with_a_full_resend(
    ws_server_factory: Callable,
) -> None:
    received: list[dict] = []

    async def handler(websocket: Any) -> None:
        async for raw in websocket:
            frame = json.loads(raw)
            received.append(frame)
            if len(received) == 1:
                await websocket.send(_completed_frame("resp_1"))
            elif len(received) == 2:
                await websocket.send(
                    json.dumps(
                        {"type": "error", "code": "previous_response_not_found", "message": "gone"}
                    )
                )
            else:
                assert "previous_response_id" not in frame
                await websocket.send(_completed_frame("resp_3"))

    await ws_server_factory(handler)
    state = CodexSessionState(session_id="s1")

    turn1_input = [{"type": "message", "role": "user", "content": []}]
    body1 = {"model": "m", "instructions": "x", "input": turn1_input}
    events1 = [
        e
        async for e in stream_ws_turn(
            credential=_CREDENTIAL, body=body1, session_id="s1", state=state
        )
    ]
    assert any(isinstance(e, Completed) for e in events1)

    new_turn2 = {"type": "message", "role": "user", "content": []}
    body2 = {"model": "m", "instructions": "x", "input": [*turn1_input, new_turn2]}
    events2 = [
        e
        async for e in stream_ws_turn(
            credential=_CREDENTIAL, body=body2, session_id="s1", state=state
        )
    ]
    assert any(isinstance(e, Completed) for e in events2)

    assert len(received) == 3
    assert received[1].get("previous_response_id") == "resp_1"
    assert "previous_response_id" not in received[2]
    await drop_connection_after_cancel(state)


@pytest.mark.asyncio
async def test_pre_stream_connection_failure_marks_session_sse_only() -> None:
    # A momentarily-bound-then-closed port: nothing listens there, so the
    # connect attempt fails before any event arrives (A.6).
    server = await websockets.serve(lambda ws: None, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    server.close()
    await server.wait_closed()

    import clio_agent.providers.codex.transport_ws as transport_ws_module

    original_url = c.CODEX_WS_URL
    try:
        transport_ws_module.c.CODEX_WS_URL = f"ws://127.0.0.1:{port}"
        state = CodexSessionState(session_id="s1")
        with pytest.raises(WsPreStreamFailure):
            async for _ in stream_ws_turn(
                credential=_CREDENTIAL,
                body={"model": "m", "input": []},
                session_id="s1",
                state=state,
            ):
                pass
        assert state.sse_only is True
        assert state.ws is None
        assert state.counters.fallbacks == 1
    finally:
        transport_ws_module.c.CODEX_WS_URL = original_url


@pytest.mark.asyncio
async def test_drop_connection_after_cancel_clears_and_closes_socket(
    ws_server_factory: Callable,
) -> None:
    async def handler(websocket: Any) -> None:
        async for raw in websocket:
            json.loads(raw)
            await websocket.send(_completed_frame("resp_1"))

    await ws_server_factory(handler)
    state = CodexSessionState(session_id="s1")
    body = {"model": "m", "instructions": "x", "input": []}
    async for _ in stream_ws_turn(credential=_CREDENTIAL, body=body, session_id="s1", state=state):
        pass
    assert state.ws is not None
    await drop_connection_after_cancel(state)
    assert state.ws is None
