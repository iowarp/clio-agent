"""Keep clio-core's control channel off the network (stopgap for iowarp/clio-core#1355).

clio-core 2.4.0 starts a control channel on every server (``ControlChannelLoop`` in
``context-runtime/src/ipc_manager.cc``) bound to ``tcp://*:<core port + 2>``. It ignores
``CLIO_BIND_ADDR``, so a loopback-pinned single-node runtime still listens on every
interface: a Windows Defender prompt on first run and a LAN-reachable port. The channel
only pings hostfile peers, and when its one bind attempt fails it logs a warning and
returns ("liveness falls back to the main sockets"); it never retries.

So before spawning the daemon, this process binds that port itself WITHOUT listening. A
bound, non-listening socket accepts nothing, and the daemon's wildcard bind fails with
EADDRINUSE. The socket is held for this process's lifetime, which outlasts the daemon's
one bind attempt (the thread starts right after the main RPC bind). Best effort: if the
port is already taken, nothing changes.

POSIX binds the wildcard address: BSD/macOS lets a ``SO_REUSEADDR`` wildcard bind coexist
with a loopback-only one. Windows binds loopback: libzmq binds with
``SO_EXCLUSIVEADDRUSE``, which any existing bind on the port defeats, and a loopback
socket raises no firewall prompt.

Skipped when ``CLIO_BIND_ADDR`` names a non-loopback address (a hand-authored multi-node
hostfile needs the channel). Remove once the pinned clio-core binds the channel to
``CLIO_BIND_ADDR``; the removal checklist is iowarp/clio-agent#2038.
"""

from __future__ import annotations

import logging
import os
import socket
import sys

from clio_agent.arc.clio_core_config import CORE_BIND_ADDR_ENV

logger = logging.getLogger(__name__)

CONTROL_PORT_OFFSET = 2
_LOOPBACK_ADDRS = frozenset({"", "127.0.0.1", "localhost", "::1"})

_held: dict[int, socket.socket] = {}


def hold_control_port(core_port: int) -> bool:
    """Occupy ``core_port + 2`` so clio-core's wildcard control bind fails.

    Returns:
        Whether this process now holds the port.
    """
    if os.environ.get(CORE_BIND_ADDR_ENV, "").strip() not in _LOOPBACK_ADDRS:
        return False
    port = core_port + CONTROL_PORT_OFFSET
    if port in _held or not 0 < port < 65536:
        return port in _held
    host = "127.0.0.1" if sys.platform.startswith("win") else "0.0.0.0"  # noqa: S104 - never listens
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.set_inheritable(False)
        sock.bind((host, port))
    except OSError as exc:
        sock.close()
        logger.debug("clio-core control port %s not held: %s", port, exc)
        return False
    _held[port] = sock
    logger.info("holding clio-core control port %s off the network (clio-core#1355)", port)
    return True
