"""How CLIO dials a view's declared service port (MA23).

A server CLIO launched may run inside the Linux fence, in a network namespace of its own,
so the loopback port it declares is that namespace's, not the host's.
:func:`fenced_target` finds the namespace: the record's serving child
(:func:`clio_agent.runtime.sandbox_net.resolve_namespace_child`) located by
:mod:`clio_agent.runtime.fence_service`. The probe, the HTTP proxy and the WebSocket proxy
then dial the declared port inside it (:func:`dial_fenced`, :class:`FencedServiceTransport`).
Without one they dial the host's loopback, as before. Only the declared port is ever dialed,
and no listener is opened for it.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import select
import socket
from typing import TYPE_CHECKING, Any

import httpcore
import httpx

from clio_agent.errors import ClioError
from clio_agent.runtime.fence_service import (
    FencedNamespace,
    connect_in_namespace,
    fenced_services_possible,
    find_fenced_namespace,
)

if TYPE_CHECKING:
    from fastapi import FastAPI

logger = logging.getLogger(__name__)

_CONNECT_TIMEOUT_S = 10.0


def _serving_child(app: FastAPI, record: Any) -> str:
    """The egress channel of the fenced child serving ``record``'s namespace, or ``""``."""

    from clio_agent.runtime.sandbox_net import resolve_namespace_child  # noqa: PLC0415

    sessions = getattr(app.state, "sessions", None)
    workspaces = getattr(app.state, "workspaces", None)
    try:
        session = sessions.get(record.session_id) if sessions is not None else None
        workspace_id = str(getattr(session, "workspace_id", "") or "")
        workspace = workspaces.get(workspace_id) if workspace_id and workspaces else None
        root = str(getattr(workspace, "root_path", "") or "")
        return resolve_namespace_child(root, getattr(record, "source_namespace", "") or "")
    except (ClioError, OSError, RuntimeError, ValueError) as exc:
        # Without a known child the namespace search is unscoped, not refused.
        logger.debug("fenced service child unknown reason=serving_child_unavailable error=%r", exc)
        return ""


async def fenced_target(app: FastAPI, record: Any) -> FencedNamespace | None:
    """The fenced namespace serving ``record``'s declared port, or ``None`` for the host's."""

    service = getattr(record, "service", None)
    if service is None or not fenced_services_possible():
        return None
    child = _serving_child(app, record)
    return await asyncio.to_thread(find_fenced_namespace, service.port, child)


async def dial_fenced(
    target: FencedNamespace, port: int, timeout: float | None = None
) -> socket.socket:
    """A socket connected to ``127.0.0.1:port`` inside ``target``.

    Raises:
        OSError: Nothing answered there, or the namespace is gone.
    """

    bound = timeout or _CONNECT_TIMEOUT_S
    return await asyncio.wait_for(
        asyncio.to_thread(connect_in_namespace, target, port, bound), timeout=bound + 10.0
    )


class _SocketStream(httpcore.AsyncNetworkStream):
    """An httpcore stream over a connected plain socket."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._reader = reader
        self._writer = writer

    async def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        try:
            return await asyncio.wait_for(self._reader.read(max_bytes), timeout)
        except TimeoutError as exc:
            raise httpcore.ReadTimeout(str(exc)) from exc
        except OSError as exc:
            raise httpcore.ReadError(str(exc)) from exc

    async def write(self, buffer: bytes, timeout: float | None = None) -> None:
        if not buffer:
            return
        try:
            self._writer.write(buffer)
            await asyncio.wait_for(self._writer.drain(), timeout)
        except TimeoutError as exc:
            raise httpcore.WriteTimeout(str(exc)) from exc
        except OSError as exc:
            raise httpcore.WriteError(str(exc)) from exc

    async def aclose(self) -> None:
        self._writer.close()
        with contextlib.suppress(OSError):
            await self._writer.wait_closed()

    def get_extra_info(self, info: str) -> Any:
        if info == "is_readable":  # an idle keep-alive that reads means the peer closed
            sock = self._writer.get_extra_info("socket")
            if self._reader.at_eof() or sock is None:
                return True
            ready, _w, _x = select.select([sock], [], [], 0)
            return bool(ready)
        if info in {"socket", "sockname", "peername"}:
            return self._writer.get_extra_info(info)
        return None


class _FencedBackend(httpcore.AsyncNetworkBackend):
    """Opens every connection to the one declared port inside the fenced namespace."""

    def __init__(self, target: FencedNamespace, port: int) -> None:
        self._target = target
        self._port = port

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Any = None,
    ) -> httpcore.AsyncNetworkStream:
        del host, port, local_address, socket_options  # always the declared port, in the fence
        try:
            sock = await dial_fenced(self._target, self._port, timeout)
        except (OSError, TimeoutError) as exc:
            raise httpcore.ConnectError(str(exc)) from exc
        reader, writer = await asyncio.open_connection(sock=sock)
        return _SocketStream(reader, writer)

    async def connect_unix_socket(
        self, path: str, timeout: float | None = None, socket_options: Any = None
    ) -> httpcore.AsyncNetworkStream:
        raise httpcore.ConnectError("a fenced service is reached over TCP only")

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


class FencedServiceTransport(httpx.AsyncHTTPTransport):
    """httpx's transport, connecting to the declared port inside the fenced namespace."""

    def __init__(self, target: FencedNamespace, port: int) -> None:
        super().__init__(trust_env=False)
        # httpx takes no network backend; its connection pool is the seam (httpx 0.28).
        self._pool = httpcore.AsyncConnectionPool(network_backend=_FencedBackend(target, port))


__all__ = ["FencedServiceTransport", "dial_fenced", "fenced_target"]
