"""Unit tests stay on this machine: any non-loopback network access fails the test.

WHY. A unit test that reaches GitHub, OpenRouter or a cloud metadata endpoint is slow,
flaky, and non-hermetic (its result depends on the network and on remote data). The
code under test usually swallows the network error and degrades, so such a test can
pass while proving something other than it claims.

WHAT. :func:`install` wraps the in-process entry points to the network:
``socket.getaddrinfo`` (every hostname lookup, including asyncio's and httpx's),
``socket.socket.connect`` / ``connect_ex`` and ``socket.create_connection``. While a
test runs (:func:`begin`), a lookup of a non-local name or a connect to a non-loopback
address raises :class:`OutboundNetworkBlocked` -- an ``OSError``, so the code under
test takes its ordinary offline path -- and is recorded; :func:`end` returns the
records, and the suite fails the test if there are any, even when the code swallowed
the error. Loopback (``127.0.0.0/8``, ``::1``, ``localhost``, the unspecified address)
stays open: local fake servers are how tests stand in for remote ones.

Scope: this process only. Subprocesses (MCP servers, CLIs) are not covered. Tests that
legitimately use the network are the ones marked ``integration``/``live``/
``real_case``/``relay``, which the unit run excludes; the guard skips them too.
"""

from __future__ import annotations

import functools
import ipaddress
import socket
import threading
import traceback
from typing import Any

EXEMPT_MARKERS = ("integration", "live", "real_case", "relay")

_LOCAL_NAMES = frozenset({"localhost", "localhost.localdomain", "ip6-localhost", ""})

_lock = threading.Lock()
_active = False
_violations: list[str] = []
_installed = False


class OutboundNetworkBlocked(OSError):
    """A unit test tried to reach a non-loopback host."""


def _host_is_local(host: object) -> bool:
    if isinstance(host, bytes):
        host = host.decode("ascii", "replace")
    if host is None:
        return True  # getaddrinfo(None, port): the local wildcard
    text = str(host).strip().strip("[]").lower()
    if text in _LOCAL_NAMES or text.endswith(".localhost"):
        return True
    try:
        address = ipaddress.ip_address(text.split("%", 1)[0])
    except ValueError:
        return False  # a hostname other than localhost: a real lookup
    return address.is_loopback or address.is_unspecified


def _check(host: object, what: str) -> None:
    if not _active or _host_is_local(host):
        return
    where = "".join(
        frame
        for frame in traceback.format_stack()[:-2]
        if "clio_agent" in frame or "tests" in frame
    )
    record = f"{what} {host!r}\n{where}"
    with _lock:
        _violations.append(record)
    raise OutboundNetworkBlocked(
        f"unit tests may not reach {host!r} ({what}); see tests/_network_guard.py"
    )


def _address_host(address: Any) -> object:
    if isinstance(address, tuple) and address:
        return address[0]
    return None  # AF_UNIX path or another family: not internet access


def install() -> None:
    """Wrap the socket entry points once per process (idempotent)."""
    global _installed
    if _installed:
        return
    real_getaddrinfo = socket.getaddrinfo
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex
    real_create_connection = socket.create_connection

    @functools.wraps(real_getaddrinfo)
    def getaddrinfo(host: Any, *args: Any, **kwargs: Any) -> Any:
        _check(host, "getaddrinfo")
        return real_getaddrinfo(host, *args, **kwargs)

    @functools.wraps(real_connect)
    def connect(self: socket.socket, address: Any) -> Any:
        if self.family in (socket.AF_INET, socket.AF_INET6):
            _check(_address_host(address), "connect")
        return real_connect(self, address)

    @functools.wraps(real_connect_ex)
    def connect_ex(self: socket.socket, address: Any) -> Any:
        if self.family in (socket.AF_INET, socket.AF_INET6):
            _check(_address_host(address), "connect")
        return real_connect_ex(self, address)

    @functools.wraps(real_create_connection)
    def create_connection(address: Any, *args: Any, **kwargs: Any) -> Any:
        _check(_address_host(address), "create_connection")
        return real_create_connection(address, *args, **kwargs)

    socket.getaddrinfo = getaddrinfo  # type: ignore[assignment]
    socket.socket.connect = connect  # type: ignore[method-assign]
    socket.socket.connect_ex = connect_ex  # type: ignore[method-assign]
    socket.create_connection = create_connection  # type: ignore[assignment]
    _installed = True


def begin() -> None:
    """Start blocking and recording for one test."""
    global _active
    with _lock:
        _violations.clear()
        _active = True


def end() -> list[str]:
    """Stop blocking; return what the test tried to reach."""
    global _active
    with _lock:
        _active = False
        found = list(_violations)
        _violations.clear()
    return found
