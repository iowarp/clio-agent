"""A minimal Agent Client Protocol v1 client for driving ``clio-coder acp`` over stdio.

The client side of ACP is small: clio-agent sends ``initialize``, ``session/new``,
``session/set_mode``, ``session/set_config_option``, ``session/prompt``, ``session/cancel`` and
``session/close``; Clio Coder streams ``session/update`` notifications and may ask
``session/request_permission``. This client advertises no ``fs`` or ``terminal`` capability, so
Clio Coder reads and writes the workspace itself, inside its own sandbox.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Awaitable, Callable
from typing import Any

from clio_agent.runtime.clio_coder.launch import (
    REASON_PROTOCOL,
    STOP_GRACE_SECONDS,
    ClioCoderError,
    SpawnedInstance,
)

ACP_PROTOCOL_VERSION = 1

#: ``(method, params)`` for every ``session/update`` and ``_clio-coder/*`` notification.
NotificationHandler = Callable[[str, dict[str, Any]], None]
#: Receives ``session/request_permission`` params; returns an offered ``optionId`` or ``None``
#: to answer ``cancelled``.
PermissionHandler = Callable[[dict[str, Any]], Awaitable[str | None]]


async def reject_permissions(params: dict[str, Any]) -> str | None:
    """The unattended default: refuse every mediated ask, as a headless run does."""
    for option in params.get("options", []):
        if option.get("kind") == "reject_once" and option.get("optionId") != "reject-and-stop":
            return option["optionId"]
    return None


async def allow_permissions_once(params: dict[str, Any]) -> str | None:
    """Approve each mediated ask once; for callers that already hold the operator's consent."""
    for option in params.get("options", []):
        if option.get("kind") == "allow_once":
            return option["optionId"]
    return None


class AcpConnection:
    """JSON-RPC 2.0 over newline-delimited stdio with one spawned ``clio-coder acp``."""

    def __init__(
        self,
        instance: SpawnedInstance,
        *,
        on_notification: NotificationHandler | None = None,
        on_permission: PermissionHandler = reject_permissions,
    ) -> None:
        self.instance = instance
        self._on_notification = on_notification
        self._on_permission = on_permission
        self._next_id = 0
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._reader: asyncio.Task[None] | None = None
        self._agent_requests: set[asyncio.Task[None]] = set()
        self.agent_capabilities: dict[str, Any] = {}
        self.agent_info: dict[str, Any] = {}

    async def start(self, client_name: str = "clio-agent", client_version: str = "0") -> None:
        self._reader = asyncio.create_task(self._read_loop(), name="clio-coder-acp-reader")
        result = await self.request(
            "initialize",
            {
                "protocolVersion": ACP_PROTOCOL_VERSION,
                "clientCapabilities": {
                    "fs": {"readTextFile": False, "writeTextFile": False},
                    "terminal": False,
                },
                "clientInfo": {"name": client_name, "version": client_version},
            },
        )
        if result.get("protocolVersion") != ACP_PROTOCOL_VERSION:
            raise ClioCoderError(
                REASON_PROTOCOL,
                "Clio Coder answered an unsupported ACP protocol version",
                {"protocolVersion": result.get("protocolVersion")},
            )
        self.agent_capabilities = result.get("agentCapabilities", {})
        self.agent_info = result.get("agentInfo", {})

    async def _write(self, message: dict[str, Any]) -> None:
        stdin = self.instance.process.stdin
        if stdin is None or stdin.is_closing():
            raise ClioCoderError(REASON_PROTOCOL, "Clio Coder ACP stdin is closed")
        stdin.write((json.dumps(message, separators=(",", ":")) + "\n").encode("utf-8"))
        await stdin.drain()

    async def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self._next_id += 1
        request_id = self._next_id
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            await self._write(
                {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
            )
            return await future
        finally:
            self._pending.pop(request_id, None)

    async def notify(self, method: str, params: dict[str, Any]) -> None:
        await self._write({"jsonrpc": "2.0", "method": method, "params": params})

    async def _read_loop(self) -> None:
        stdout = self.instance.process.stdout
        if stdout is None:
            raise ClioCoderError(REASON_PROTOCOL, "Clio Coder ACP stdout is not piped")
        while line := await stdout.readline():
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                self._fail_pending(
                    ClioCoderError(
                        REASON_PROTOCOL,
                        "Clio Coder wrote a non-JSON line on the ACP channel",
                        {"line": line[:200].decode("utf-8", "replace")},
                    )
                )
                return
            self._dispatch(message)
        self._fail_pending(
            ClioCoderError(
                REASON_PROTOCOL,
                "Clio Coder closed the ACP channel",
                {"exitCode": self.instance.process.returncode},
            )
        )

    def _dispatch(self, message: dict[str, Any]) -> None:
        if "method" in message and "id" in message:
            task = asyncio.create_task(self._answer_agent_request(message))
            self._agent_requests.add(task)
            task.add_done_callback(self._agent_requests.discard)
        elif "method" in message:
            if self._on_notification is not None:
                self._on_notification(message["method"], message.get("params") or {})
        elif (future := self._pending.get(message.get("id"))) is not None and not future.done():
            if "error" in message:
                error = message["error"] or {}
                future.set_exception(
                    ClioCoderError(
                        REASON_PROTOCOL,
                        f"Clio Coder refused {error.get('message', 'the request')}",
                        {"code": error.get("code"), "data": error.get("data")},
                    )
                )
            else:
                future.set_result(message.get("result") or {})

    async def _answer_agent_request(self, message: dict[str, Any]) -> None:
        if message["method"] == "session/request_permission":
            option = await self._on_permission(message.get("params") or {})
            outcome = (
                {"outcome": "selected", "optionId": option}
                if option is not None
                else {"outcome": "cancelled"}
            )
            reply: dict[str, Any] = {"result": {"outcome": outcome}}
        else:
            reply = {"error": {"code": -32601, "message": f"{message['method']} is not served"}}
        # A server that already exited cannot take the reply; the closed channel itself is
        # reported to every pending request by the reader.
        with contextlib.suppress(ClioCoderError, ConnectionError):
            await self._write({"jsonrpc": "2.0", "id": message["id"], **reply})

    def _fail_pending(self, error: ClioCoderError) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(error)

    async def close(self) -> None:
        """Close stdin so the server drains, then settle the reader and owned tree."""
        stdin = self.instance.process.stdin
        if stdin is not None and not stdin.is_closing():
            stdin.close()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self.instance.process.wait(), STOP_GRACE_SECONDS)
        await self.instance.stop()
        if self._reader is not None:
            with contextlib.suppress(ClioCoderError):
                await self._reader
        for task in list(self._agent_requests):
            task.cancel()
