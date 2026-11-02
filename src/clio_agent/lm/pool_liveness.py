"""Idle-connection liveness for DSPy's pooled lm15 HTTP transports.

lm15 (vendored by the pinned DSPy) decides whether an idle keep-alive connection may be
reused with ``select.select([sock], [], [], 0)``. ``select`` cannot watch a descriptor
numbered at or above ``FD_SETSIZE`` (1024): it raises ``ValueError``, which lm15 treats as
"stale". A long-lived process with more than ~1024 open descriptors (a GACT server, or a
test worker late in a full suite) therefore silently discards every pooled connection and
opens a new TCP/TLS connection for each request, while the discarded socket's close is
still settling at the peer.

This module replaces only that probe. It keeps lm15's decision unchanged — an idle socket
that is readable (EOF, ``close_notify``, unread bytes) or in error/hang-up is not reusable —
and uses ``poll(2)``, which has no descriptor-number ceiling. Windows has no ``poll``; its
``select`` is limited by the number of sockets, not their value, so it keeps ``select``.

A probe that genuinely fails still refuses reuse (the safe outcome), but it is no longer
silent: it emits the typed ``lm.pool.liveness_probe_failed`` reason.
"""

from __future__ import annotations

import logging
import select
import socket
import threading
from importlib import import_module
from typing import Any

__all__ = ["idle_socket_unusable", "install_pool_liveness"]

logger = logging.getLogger(__name__)

_INSTALL_LOCK = threading.Lock()
_INSTALLED = False
#: Readable, urgent data, error, hang-up or invalid descriptor: never reuse the socket.
_UNUSABLE = (
    getattr(select, "POLLIN", 0)
    | getattr(select, "POLLPRI", 0)
    | getattr(select, "POLLERR", 0)
    | getattr(select, "POLLHUP", 0)
    | getattr(select, "POLLNVAL", 0)
)


def _probe_failed(transport: str, error: BaseException) -> bool:
    """Record a failed liveness probe as a typed reason; the connection is not reused."""
    from clio_agent.runtime.stream_audit import stream_audit  # noqa: PLC0415

    logger.warning(
        "lm pool liveness probe failed transport=%s error_type=%s",
        transport,
        type(error).__name__,
    )
    stream_audit(
        "lm.pool.liveness_probe_failed",
        transport=transport,
        reason="liveness_probe_failed",
        error_type=type(error).__name__,
    )
    return True


def idle_socket_unusable(sock: Any, transport: str) -> bool:
    """Return whether an idle pooled socket must be discarded instead of reused.

    Args:
        sock: The connection's socket (``socket.socket``, ``ssl.SSLSocket`` or asyncio's
            ``TransportSocket``), or ``None`` when the transport no longer has one.
        transport: ``"sync"`` or ``"async"``, carried into a failure reason.

    Returns:
        ``True`` when the peer closed, sent unexpected bytes, or the socket is gone.
    """
    if sock is None:
        return True
    try:
        fd = sock.fileno()
        if fd < 0:  # closed underneath the pool; poll() would silently ignore it
            return True
        if not hasattr(select, "poll"):  # Windows: select limits count, not fd value
            readable, _, errored = select.select([sock], [], [sock], 0)
            return bool(readable or errored)
        poller = select.poll()
        poller.register(fd, _UNUSABLE)
        return bool(poller.poll(0))
    except (OSError, ValueError) as exc:
        return _probe_failed(transport, exc)


def _sync_is_stale(self: Any) -> bool:
    if self.closed:
        return True
    return idle_socket_unusable(self.sock, "sync")


def _async_is_stale(self: Any) -> bool:
    if self.closed:
        return True
    try:
        sock: socket.socket | None = self.writer.get_extra_info("socket")
    except Exception as exc:  # noqa: BLE001 - forwards: _probe_failed typed reason
        return _probe_failed("async", exc)
    return idle_socket_unusable(sock, "async")


def install_pool_liveness() -> None:
    """Install the descriptor-safe probe on lm15's sync and async pooled connections.

    Idempotent. The private classes are part of the pinned DSPy boundary; their presence
    and the installed probe are asserted by ``tests/test_lm/test_pool_liveness.py``.
    """
    global _INSTALLED  # noqa: PLW0603
    if _INSTALLED:
        return
    with _INSTALL_LOCK:
        if _INSTALLED:
            return
        sync = import_module("dspy._vendor.lm15.transports._sync")
        asynchronous = import_module("dspy._vendor.lm15.transports._async")
        sync._SyncConnection.is_stale = _sync_is_stale
        asynchronous._AsyncConnection.is_stale = _async_is_stale
        _INSTALLED = True
