"""Desktop-attached execution transport used by local CLIO SSH targets."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import suppress
from uuid import uuid4

from fastapi import WebSocket, WebSocketDisconnect

from clio_agent.gact.infrastructure.models import CommandResult, CommandSpec


class TransportUnavailableError(RuntimeError):
    """The target has no connected Desktop transport."""


class _Connection:
    def __init__(self, websocket: WebSocket) -> None:
        self.websocket = websocket
        self.send_lock = asyncio.Lock()
        self.pending: dict[str, asyncio.Future[dict[str, object]]] = {}

    async def request(self, payload: dict[str, object], *, timeout: float) -> dict[str, object]:
        request_id = str(uuid4())
        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, object]] = loop.create_future()
        self.pending[request_id] = future
        try:
            async with self.send_lock:
                await self.websocket.send_json({**payload, "request_id": request_id})
            return await asyncio.wait_for(future, timeout=timeout)
        finally:
            self.pending.pop(request_id, None)

    def resolve(self, payload: dict[str, object]) -> None:
        request_id = str(payload.get("request_id") or "")
        future = self.pending.get(request_id)
        if future is not None and not future.done():
            future.set_result(payload)

    def fail_pending(self, message: str) -> None:
        for future in self.pending.values():
            if not future.done():
                future.set_exception(TransportUnavailableError(message))


class InfrastructureTransportRegistry:
    """Map durable target ids to live, Desktop-owned SSH transport streams."""

    def __init__(self, on_state: Callable[[str, str], None]) -> None:
        self._connections: dict[str, _Connection] = {}
        self._forwards: dict[tuple[str, int], str] = {}
        self._lock = asyncio.Lock()
        self._on_state = on_state

    def connected(self, target_id: str) -> bool:
        """Return whether a Desktop bridge is currently attached."""

        return target_id in self._connections

    async def serve(self, target_id: str, websocket: WebSocket) -> None:
        """Attach one WebSocket until its Desktop transport disconnects."""

        await websocket.accept()
        connection = _Connection(websocket)
        async with self._lock:
            previous = self._connections.pop(target_id, None)
            if previous is not None:
                previous.fail_pending("A replacement Desktop transport was attached")
                with suppress(Exception):
                    await previous.websocket.close(code=1012)
            self._connections[target_id] = connection
            self._forwards = {
                key: value for key, value in self._forwards.items() if key[0] != target_id
            }
        self._on_state(target_id, "connected")
        try:
            while True:
                payload = await websocket.receive_json()
                if isinstance(payload, dict):
                    connection.resolve(payload)
        except (WebSocketDisconnect, RuntimeError, ValueError):
            pass
        finally:
            async with self._lock:
                if self._connections.get(target_id) is connection:
                    self._connections.pop(target_id, None)
                self._forwards = {
                    key: value for key, value in self._forwards.items() if key[0] != target_id
                }
            connection.fail_pending("Desktop SSH transport disconnected")
            self._on_state(target_id, "disconnected")

    async def execute(self, target_id: str, spec: CommandSpec) -> CommandResult:
        """Execute one CLIO-generated command through an attached Desktop bridge."""

        connection = self._connections.get(target_id)
        if connection is None:
            raise TransportUnavailableError("Desktop SSH transport is not connected")
        payload = await connection.request(
            {"type": "exec", "command": spec.model_dump(mode="json")},
            timeout=spec.timeout_seconds + 10,
        )
        if payload.get("type") != "exec_result":
            raise RuntimeError("Desktop transport returned an invalid execution response")
        return CommandResult.model_validate(payload.get("result"))

    async def forward(
        self,
        target_id: str,
        remote_port: int,
        preferred_local_port: int | None = None,
    ) -> str:
        """Request a restorable loopback forward through the existing SSH session."""

        connection = self._connections.get(target_id)
        if connection is None:
            raise TransportUnavailableError("Desktop SSH transport is not connected")
        cached = self._forwards.get((target_id, remote_port))
        if cached is not None:
            return cached
        payload = await connection.request(
            {
                "type": "forward",
                "remote_host": "127.0.0.1",
                "remote_port": remote_port,
                "local_port": preferred_local_port,
            },
            timeout=20,
        )
        if payload.get("type") != "forward_result" or not isinstance(payload.get("local_url"), str):
            raise RuntimeError("Desktop transport returned an invalid forwarding response")
        url = str(payload["local_url"])
        self._forwards[(target_id, remote_port)] = url
        return url

    async def detach(self, target_id: str) -> None:
        """Close one live attachment without deleting its durable target."""

        async with self._lock:
            connection = self._connections.pop(target_id, None)
        if connection is None:
            return
        connection.fail_pending("Desktop SSH transport was detached")
        self._forwards = {
            key: value for key, value in self._forwards.items() if key[0] != target_id
        }
        with suppress(Exception):
            await connection.websocket.close(code=1000)
        self._on_state(target_id, "disconnected")
