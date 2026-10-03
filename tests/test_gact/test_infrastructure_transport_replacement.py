"""A closing socket cannot invalidate a replacement Desktop bridge."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from fastapi import WebSocketDisconnect

from clio_agent.gact.infrastructure.transport import InfrastructureTransportRegistry


class Socket:
    """Controllable admitted transport with delayed closing, for the race test."""

    def __init__(self) -> None:
        self.headers: dict[str, str] = {"sec-websocket-protocol": "clio.infrastructure.v2"}
        self.scope = {"subprotocols": ["clio.infrastructure.v2"]}
        self.closed = asyncio.Event()
        self.attached = asyncio.Event()

    async def accept(self, **kwargs: Any) -> None:
        pass

    async def send_json(self, payload: dict[str, Any]) -> None:
        self.attached.set()

    async def receive_json(self) -> dict[str, Any]:
        await self.closed.wait()
        raise WebSocketDisconnect()

    async def close(self, **kwargs: Any) -> None:
        # The old socket's receive loop is deliberately still alive.
        pass


@pytest.mark.asyncio
async def test_old_socket_cleanup_preserves_new_attachment_and_forward() -> None:
    """Reproduce a replacement completing before the old receive loop exits."""

    states: list[tuple[str, str]] = []
    registry = InfrastructureTransportRegistry(lambda target, state: states.append((target, state)))
    old, new = Socket(), Socket()
    first = asyncio.create_task(registry.serve("hpc", old))  # type: ignore[arg-type]
    await asyncio.wait_for(old.attached.wait(), 1)
    second = asyncio.create_task(registry.serve("hpc", new))  # type: ignore[arg-type]
    await asyncio.wait_for(new.attached.wait(), 1)
    registry._forwards[("hpc", 17800)] = "http://127.0.0.1:40000"
    old.closed.set()
    await first
    assert registry.connected("hpc")
    assert states[-1] == ("hpc", "connected")
    assert await registry.forward("hpc", 17800) == "http://127.0.0.1:40000"
    new.closed.set()
    await second
    assert states[-1] == ("hpc", "disconnected")
