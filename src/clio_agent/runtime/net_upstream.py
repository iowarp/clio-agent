"""Chaining the network chokepoint through the site proxy CLIO itself was given.

On networks without direct egress (e.g. HPC compute nodes) a confined child's connection is
dialled through ``*_proxy`` / ``no_proxy``, so it reaches the same hosts CLIO can.
:mod:`clio_agent.runtime.net_chokepoint` dials every upstream through here.
"""

from __future__ import annotations

import contextlib
import socket
import urllib.parse
import urllib.request
from typing import Optional


def _upstream_proxy(host: str, scheme: str) -> Optional[tuple[str, int]]:
    """The site proxy CLIO itself must use to reach ``host`` (``*_proxy``/``no_proxy``), or None.

    On networks without direct egress (e.g. HPC compute nodes) the chokepoint chains through
    the proxy the CLIO process was given, so a confined child reaches the same hosts CLIO can.
    """
    proxies = urllib.request.getproxies_environment()
    url = proxies.get(scheme) or proxies.get("https" if scheme == "http" else "http")
    url = url or proxies.get("all")
    if not url or urllib.request.proxy_bypass_environment(host):
        return None
    parsed = urllib.parse.urlsplit(url if "://" in url else "http://" + url)
    if not parsed.hostname:
        return None
    return parsed.hostname, parsed.port or 8080


def _dial_tunnel(host: str, port: int, timeout: float = 30.0) -> socket.socket:
    """A socket carrying bytes to ``host:port``: direct, or CONNECT through the site proxy."""
    relay = _upstream_proxy(host, "https")
    if relay is None:
        return socket.create_connection((host, port), timeout=timeout)
    upstream = socket.create_connection(relay, timeout=timeout)
    try:
        upstream.sendall(f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n\r\n".encode())
        reply = b""
        while b"\r\n\r\n" not in reply and len(reply) < 16 * 1024:
            chunk = upstream.recv(4096)
            if not chunk:
                break
            reply += chunk
        status = reply.split(b"\r\n", 1)[0].split()
        if len(status) < 2 or status[1] != b"200":
            raise OSError(f"site proxy refused CONNECT {host}:{port}: {reply[:80]!r}")
    except BaseException:
        with contextlib.suppress(OSError):
            upstream.close()
        raise
    return upstream
