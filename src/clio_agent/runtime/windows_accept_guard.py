"""Keep asyncio proactor servers listening when a Windows client aborts mid-accept.

A client that resets its connection while ``AcceptEx`` is pending completes the
accept with ``ERROR_NETNAME_DELETED`` (WinError 64). CPython's
``BaseProactorEventLoop._start_serving`` treats any such ``OSError`` as fatal:
it reports "Accept failed on a socket" and closes the listening socket, so the
process keeps running but never accepts again (python/cpython#93821). Browsers
cancel requests routinely, so this deafens a uvicorn server on Windows.

:func:`install` replaces ``IocpProactor.accept`` with one that closes the
half-open socket and re-arms ``AcceptEx`` on transient client-side accept
errors, so the server keeps accepting. It does nothing off Windows, nor on
CPython releases that carry the upstream fix (python/cpython#161238, backported
in #161309 and #161308).

The Desktop GACT server installs it before uvicorn starts serving
(:mod:`clio_agent.gact.desktop_lifecycle`).
"""

from __future__ import annotations

import logging
import sys
from typing import Any

# ERROR_NETNAME_DELETED, ERROR_CONNECTION_ABORTED, WSAECONNABORTED, WSAECONNRESET.
_TRANSIENT_WINERRORS = frozenset({64, 1236, 10053, 10054})
_GUARDED = "_clio_accept_guard"

logger = logging.getLogger("clio_agent.windows_accept_guard")


def upstream_fixed(version: tuple[Any, ...] | None = None) -> bool:
    """Whether this CPython already keeps serving after a client aborts mid-accept."""
    v = tuple(version if version is not None else sys.version_info)
    if v[:2] == (3, 14):
        return v[:3] >= (3, 14, 11)
    if v[:2] == (3, 15):
        return v[:3] >= (3, 15, 3)
    if v[:2] == (3, 16):
        return v[:3] > (3, 16, 0) or v[3:5] != ("alpha", 1)
    # 3.12 and 3.13 only receive security releases and will not get the fix.
    return v[:2] > (3, 16)


def transient(exc: BaseException) -> bool:
    """Whether an accept failed only because the client went away first."""
    if isinstance(exc, (ConnectionResetError, ConnectionAbortedError)):
        return True
    return isinstance(exc, OSError) and getattr(exc, "winerror", None) in _TRANSIENT_WINERRORS


def install() -> str:
    """Guard ``IocpProactor.accept``; return what was done.

    One of ``not_windows``, ``upstream_fixed``, ``unsupported`` (the private
    proactor API it relies on has changed), ``already_installed`` or
    ``installed``. Safe to call more than once.
    """
    if sys.platform != "win32":
        return "not_windows"
    if upstream_fixed():
        return "upstream_fixed"
    try:
        import _overlapped
        from asyncio import windows_events
    except ImportError:
        return "unsupported"
    proactor = windows_events.IocpProactor
    needed = ("accept", "_register", "_register_with_iocp", "_get_accept_socket")
    if not all(hasattr(proactor, name) for name in needed) or not hasattr(
        _overlapped, "SO_UPDATE_ACCEPT_CONTEXT"
    ):
        return "unsupported"
    if getattr(proactor.accept, _GUARDED, False):
        return "already_installed"
    guarded = _guarded_accept(windows_events, _overlapped)
    setattr(guarded, _GUARDED, True)
    proactor.accept = guarded
    return "installed"


def _guarded_accept(windows_events: Any, overlapped: Any) -> Any:
    import asyncio
    import socket
    import struct

    def accept_once(proactor: Any, listener: socket.socket) -> Any:
        """One ``AcceptEx``, as CPython does it, but never leaking the accept socket."""
        proactor._register_with_iocp(listener)
        conn = proactor._get_accept_socket(listener.family)
        ov = overlapped.Overlapped(windows_events.NULL)
        try:
            ov.AcceptEx(listener.fileno(), conn.fileno())
        except BaseException:
            conn.close()
            raise

        def finish_accept(trans: Any, key: Any, ov: Any) -> tuple[socket.socket, Any]:
            try:
                ov.getresult()
                # SO_UPDATE_ACCEPT_CONTEXT makes getsockname() etc. work.
                buf = struct.pack("@P", listener.fileno())
                conn.setsockopt(socket.SOL_SOCKET, overlapped.SO_UPDATE_ACCEPT_CONTEXT, buf)
                conn.settimeout(listener.gettimeout())
                return conn, conn.getpeername()
            except BaseException:
                conn.close()
                raise

        future = proactor._register(ov, listener, finish_accept)
        future.add_done_callback(lambda done: conn.close() if done.cancelled() else None)
        return future

    class AcceptFuture(asyncio.Future):  # type: ignore[type-arg]
        """Cancels the pending ``AcceptEx`` synchronously, as CPython's own future does:
        ``_stop_serving`` closes the listener right after cancelling it."""

        inner: Any = None

        def cancel(self, msg: Any = None) -> bool:
            if self.inner is not None and not self.inner.done():
                self.inner.cancel()
            return super().cancel(msg)

    def accept(self: Any, listener: socket.socket) -> Any:
        outer = AcceptFuture(loop=self._loop)

        def arm() -> None:
            outer.inner = accept_once(self, listener)
            outer.inner.add_done_callback(settle)

        def settle(inner: Any) -> None:
            if inner.cancelled():
                outer.cancel()
                return
            exc = inner.exception()
            if outer.done():
                if exc is None:
                    inner.result()[0].close()
                return
            if exc is None:
                outer.set_result(inner.result())
            elif transient(exc) and listener.fileno() != -1:
                logger.debug("accept_aborted_by_client rearming error=%r", exc)
                try:
                    arm()
                except Exception as again:  # noqa: BLE001 - forwards: the caller's future
                    outer.set_exception(again)
            else:
                outer.set_exception(exc)

        arm()
        return outer

    return accept
