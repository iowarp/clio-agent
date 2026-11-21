"""Refuse a daemon spawn whose IPC socket path cannot be bound (owner module).

clio-core keeps its IPC socket, segment links and pid record in a per-user directory:
``$CLIO_MEMFD_DIR`` when set, else ``/tmp/clio_$USER`` (``%TEMP%\\clio_%USERNAME%``
on Windows). The socket is ``<dir>/clio_<port>.ipc``. A Unix-domain socket path must
fit ``sockaddr_un.sun_path`` (108 bytes on Linux and Windows, 104 on macOS and the
BSDs, both including the terminating NUL).

From iowarp-core 2.5.0 the daemon refuses to start "half-bound" when that listen
fails: it exits with status 1, and its only words are one ERROR line. A long
``USER`` gets there, and CLIO itself derives one for a per-home daemon
(``<user>-clio-<16 hex>``, :func:`clio_agent.arc.home_runtime.core_user`), and
``CLIO_CORE_USER`` / ``CLIO_MEMFD_DIR`` are free-form. This module computes the path
the daemon will use from the child's own environment and raises a typed
:class:`ClioCoreIpcPathTooLong` before the spawn, so the failure names its cause
instead of surfacing as a crash with an empty log tail.
"""

from __future__ import annotations

import os
import sys
import tempfile
from collections.abc import Mapping

from clio_agent.arc.runtime_crash import DaemonSpawnFailed

CLIO_CORE_IPC_PATH_TOO_LONG = "clio_core_ipc_path_too_long"

#: ``sizeof(sockaddr_un::sun_path)`` per platform; the path must be strictly shorter.
_SUN_PATH_LINUX_WINDOWS = 108
_SUN_PATH_BSD = 104


def sun_path_capacity(platform: str | None = None) -> int:
    """Size of ``sockaddr_un.sun_path`` on ``platform`` (default: this host)."""
    name = platform or sys.platform
    if name == "darwin" or "bsd" in name:
        return _SUN_PATH_BSD
    return _SUN_PATH_LINUX_WINDOWS


def memfd_dir(env: Mapping[str, str], platform: str | None = None) -> str:
    """The per-user directory clio-core will use for ``env`` (mirrors ``GetMemfdDir``)."""
    override = env.get("CLIO_MEMFD_DIR", "")
    if override:
        return override
    name = platform or sys.platform
    if name.startswith("win"):
        user = env.get("USERNAME") or "unknown"
        return os.path.join(tempfile.gettempdir(), f"clio_{user}")
    user = env.get("USER") or "unknown"
    return f"/tmp/clio_{user}"


def ipc_socket_path(env: Mapping[str, str], port: int, platform: str | None = None) -> str:
    """``<memfd dir>/clio_<port>.ipc``, the socket the daemon listens on."""
    return f"{memfd_dir(env, platform)}/clio_{port}.ipc"


class ClioCoreIpcPathTooLong(DaemonSpawnFailed):
    """The daemon's IPC socket path does not fit ``sun_path``; the daemon would exit 1."""

    degradation_reason = CLIO_CORE_IPC_PATH_TOO_LONG

    def __init__(self, path: str, capacity: int) -> None:
        self.path = path
        self.capacity = capacity
        super().__init__(
            f"clio-core cannot start (reason={CLIO_CORE_IPC_PATH_TOO_LONG}): its IPC socket "
            f"path {path!r} is {len(path.encode('utf-8'))} bytes, and a Unix socket path on "
            f"this platform must be under {capacity}. The directory comes from CLIO_MEMFD_DIR, "
            "else the USER (USERNAME on Windows) the daemon sees, which CLIO_CORE_USER can set. "
            "Set CLIO_MEMFD_DIR to a short private directory (mode 0700) or use a shorter "
            "CLIO_CORE_USER."
        )


def require_ipc_path_fits(env: Mapping[str, str], port: int, platform: str | None = None) -> None:
    """Raise :class:`ClioCoreIpcPathTooLong` when the daemon could not bind its socket."""
    path = ipc_socket_path(env, port, platform)
    capacity = sun_path_capacity(platform)
    if len(path.encode("utf-8")) >= capacity:
        raise ClioCoreIpcPathTooLong(path, capacity)
