"""Which local account opened a loopback connection to this server.

Loopback HTTP is admitted without the bearer token, so that the browser,
``clio`` and the Desktop on CLIO's own computer need nothing extra. Every
account on the machine can reach loopback, though, and on a shared host such as
an HPC login node that includes other users. On Linux the kernel records the
owner of each TCP socket in ``/proc/net/tcp`` and ``/proc/net/tcp6``; this
module finds the peer's socket there so the server can admit only its own user
without a token.

Elsewhere (macOS, Windows) there is no such table to read, and the check is
skipped.
"""

from __future__ import annotations

import ipaddress
import os
import struct
import sys
from collections.abc import Iterator
from pathlib import Path

from starlette.types import Scope

_PROC_TABLES = (Path("/proc/net/tcp"), Path("/proc/net/tcp6"))

Endpoint = tuple[ipaddress.IPv4Address | ipaddress.IPv6Address, int]


def _normalize(
    address: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    mapped = getattr(address, "ipv4_mapped", None)
    return mapped if mapped is not None else address


def _decode_endpoint(text: str) -> Endpoint:
    """Decode a ``/proc/net/tcp{,6}`` address column such as ``0100007F:1F90``."""

    host, port = text.split(":")
    raw = bytes.fromhex(host)
    # The kernel prints each 32-bit word of the address in host byte order.
    words = struct.unpack(f"={len(raw) // 4}I", raw)
    packed = struct.pack(f">{len(words)}I", *words)
    address = ipaddress.ip_address(packed)
    return _normalize(address), int(port, 16)


def _socket_table_rows(tables: tuple[Path, ...]) -> Iterator[tuple[Endpoint, Endpoint, int]]:
    for table in tables:
        try:
            lines = table.read_text().splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            fields = line.split()
            if len(fields) < 8:
                continue
            try:
                yield _decode_endpoint(fields[1]), _decode_endpoint(fields[2]), int(fields[7])
            except ValueError:
                continue


def _endpoint(value: object) -> Endpoint | None:
    if not isinstance(value, (tuple, list)) or len(value) < 2:
        return None
    try:
        return _normalize(ipaddress.ip_address(str(value[0]))), int(value[1])
    except ValueError:
        return None


def socket_owner_uid(
    client: Endpoint, server: Endpoint, *, tables: tuple[Path, ...] = _PROC_TABLES
) -> int | None:
    """The uid owning the client end of the TCP connection ``client`` → ``server``.

    Returns:
        The uid, or ``None`` if no socket table lists the connection.
    """

    for local, remote, uid in _socket_table_rows(tables):
        if local == client and remote == server:
            return uid
    return None


def loopback_peer_is_server_user(scope: Scope) -> bool:
    """Whether a loopback peer's socket belongs to the user this server runs as.

    Without a TCP connection to look up (an in-process ASGI client) or on a
    platform with no socket table, there is nothing to check and the answer is
    ``True``. On Linux a connection that cannot be found is treated as
    another user's: its socket may already have been closed.
    """

    client = _endpoint(scope.get("client"))
    server = _endpoint(scope.get("server"))
    if client is None or server is None or not sys.platform.startswith("linux"):
        return True
    return socket_owner_uid(client, server) == os.geteuid()
