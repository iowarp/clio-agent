"""Exercise the server's default WebSocket protocol against a real local client."""

from __future__ import annotations

import asyncio
import socket

import pytest
import uvicorn
from fastapi import FastAPI, WebSocket
from websockets.asyncio.client import connect


@pytest.mark.asyncio
async def test_default_server_websocket_transport_round_trip() -> None:
    """The supported default protocol must preserve text, binary data, and clean closes."""
    app = FastAPI()

    @app.websocket("/echo")
    async def echo(websocket: WebSocket) -> None:
        await websocket.accept()
        await websocket.send_text(await websocket.receive_text())
        await websocket.send_bytes(await websocket.receive_bytes())
        await websocket.close()

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        listener.setblocking(False)
        port = listener.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(app, log_level="warning", lifespan="off"))
        serving = asyncio.create_task(server.serve(sockets=[listener]))
        try:
            async with asyncio.timeout(10):
                while not server.started:
                    if serving.done():
                        serving.result()
                    await asyncio.sleep(0.01)
            async with connect(f"ws://127.0.0.1:{port}/echo", open_timeout=5) as client:
                await client.send("CLIO transport \u2713")
                assert await asyncio.wait_for(client.recv(), timeout=5) == "CLIO transport \u2713"
                payload = bytes(range(256)) * 256
                await client.send(payload)
                assert await asyncio.wait_for(client.recv(), timeout=5) == payload
                await asyncio.wait_for(client.wait_closed(), timeout=5)
                assert client.close_code == 1000
        finally:
            server.should_exit = True
            await asyncio.wait_for(serving, timeout=10)
