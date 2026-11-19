"""What a chokepoint connection is aimed at: its parsed target, address kind and dial.

:mod:`clio_agent.runtime.net_chokepoint` reads a client's request head with these, decides
whether the target is the relay sentinel or a non-public address that needs consent, dials
it, and refuses an upstream that turns out to be one of CLIO's own listeners.
"""

from __future__ import annotations

import ipaddress
import logging
import socket
from typing import Any, Optional

from clio_agent.runtime.net_connect import RELAY_HOST, RELAY_PORT

logger = logging.getLogger(__name__)

#: Non-public address kinds of a relayed target (``EgressRecord.network``), strictest first.
NETWORK_LOOPBACK = "loopback"
NETWORK_LINK_LOCAL = "link-local"
NETWORK_PRIVATE = "private"
_NETWORK_RANK = {NETWORK_LOOPBACK: 3, NETWORK_LINK_LOCAL: 2, NETWORK_PRIVATE: 1, "": 0}


def _is_relay(target: tuple[str, int]) -> bool:
    """Whether a CONNECT target is the relay sentinel (see :mod:`net_connect`)."""
    host, port = target
    if port != RELAY_PORT:
        return False
    try:
        return ipaddress.ip_address(host.strip("[]")) == ipaddress.ip_address(RELAY_HOST)
    except ValueError:
        return False


def _address_network(ip: str) -> str:
    """The non-public kind of one address (``""`` for a public one)."""
    try:
        address: Any = ipaddress.ip_address(ip.split("%", 1)[0])
    except ValueError:
        return NETWORK_PRIVATE
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    if address.is_loopback:
        return NETWORK_LOOPBACK
    if address.is_link_local:
        return NETWORK_LINK_LOCAL
    return "" if address.is_global else NETWORK_PRIVATE


def _resolve_target(host: str, port: int) -> tuple[list[tuple[int, Any]], str]:
    """``(addresses, network)`` for a relayed target: its addresses and their strictest kind.

    An unresolvable host gives ``([], "")``: the dial then fails, so nothing is reached.
    """
    try:
        infos = socket.getaddrinfo(host.strip("[]"), port, type=socket.SOCK_STREAM)
    except OSError:
        return [], ""
    addresses = [(family, sockaddr) for family, _kind, _proto, _name, sockaddr in infos]
    network = ""
    for _family, sockaddr in addresses:
        kind = _address_network(str(sockaddr[0]))
        if _NETWORK_RANK[kind] > _NETWORK_RANK[network]:
            network = kind
    return addresses, network


def _dial_addresses(addresses: list[tuple[int, Any]], timeout: float) -> socket.socket:
    """Connect to the first of ``addresses`` that answers."""
    error: OSError = OSError("no address to dial")
    for family, sockaddr in addresses:
        sock = socket.socket(family, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        try:
            sock.connect(sockaddr)
        except OSError as exc:
            error = exc
            sock.close()
            continue
        return sock
    raise error


def _reaches_clio_listener(upstream: socket.socket) -> bool:
    """Whether ``upstream`` reached a port this CLIO process listens on, through loopback.

    A fenced child must never reach CLIO's own server or proxy, which could answer its own
    permission requests. When the listening ports cannot be read, loopback counts as CLIO's.
    """
    try:
        ip, port = upstream.getpeername()[:2]
    except OSError:
        return False
    if _address_network(str(ip)) != NETWORK_LOOPBACK:
        return False
    import psutil  # noqa: PLC0415

    try:
        process = psutil.Process()
        listing = getattr(process, "net_connections", None) or process.connections
        ports = {
            conn.laddr.port
            for conn in listing(kind="inet")
            if conn.status == psutil.CONN_LISTEN and conn.laddr
        }
    except (psutil.Error, OSError) as exc:
        logger.warning("net egress listener check failed (refusing loopback) error=%r", exc)
        return True
    return int(port) in ports


def _parse_connect_target(header: bytes) -> Optional[tuple[str, int]]:
    """Parse ``CONNECT host:port HTTP/1.1`` → ``(host, port)``; ``None`` when not a CONNECT.

    CONNECT is the tunnel every HTTPS client opens through a proxy — the domain-level
    visibility tier (no MITM). A malformed target or a non-CONNECT verb returns ``None``.
    """
    try:
        line = header.split(b"\r\n", 1)[0].decode("latin-1")
    except (IndexError, UnicodeDecodeError):
        return None
    parts = line.split()
    if len(parts) < 2 or parts[0].upper() != "CONNECT":
        return None
    authority = parts[1]
    host, _, port_s = authority.rpartition(":")
    if not host or not port_s.isdigit():
        return None
    port = int(port_s)
    if not (0 < port < 65536):
        return None
    return host, port


def _parse_absolute_form(header: bytes) -> Optional[tuple[str, int, bytes, str]]:
    """Parse a proxied plain-HTTP request → ``(host, port, origin_head, method)``; ``None`` if not.

    A client configured with ``HTTP_PROXY`` sends the request line in absolute form —
    ``GET http://host:port/path HTTP/1.1`` — to the proxy. This rewrites the head to the
    origin form the target server expects (``GET /path HTTP/1.1``), preserving the method,
    the rest of the path/query, the HTTP version and every header (incl. ``Host``), so the
    proxy is a transparent passthrough. Non-HTTP-URL targets (or a malformed head) → ``None``.
    Only the request HEAD is rewritten; any body streams verbatim through the chokepoint's pump.

    ``method`` is returned UPPERCASED (the write-gate classification token, N2) while the
    rewritten ``origin_head`` keeps the verb VERBATIM — the passthrough never mutates the wire.
    """
    try:
        head_text = header.decode("latin-1")
    except UnicodeDecodeError:
        return None
    line, sep, rest = head_text.partition("\r\n")
    if not sep:
        return None
    parts = line.split(" ")
    if len(parts) != 3:
        return None
    method, target, version = parts
    if not target.lower().startswith("http://"):
        return None
    without_scheme = target[len("http://") :]
    authority, slash, path = without_scheme.partition("/")
    origin_path = f"/{path}" if slash else "/"
    host, _, port_s = authority.partition(":")
    if not host:
        return None
    if port_s:
        if not port_s.isdigit() or not (0 < int(port_s) < 65536):
            return None
        port = int(port_s)
    else:
        port = 80
    origin_head = f"{method} {origin_path} {version}\r\n{rest}".encode("latin-1")
    return host, port, origin_head, method.upper()
