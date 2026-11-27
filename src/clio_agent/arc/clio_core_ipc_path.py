"""Refuse a daemon spawn whose IPC sockets cannot be bound (owner module).

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
import stat
import sys
import tempfile
from collections.abc import Mapping

from clio_agent.arc.runtime_crash import DaemonSpawnFailed

CLIO_CORE_IPC_PATH_TOO_LONG = "clio_core_ipc_path_too_long"
CLIO_CORE_MEMFD_DIR_UNSAFE = "clio_core_memfd_dir_unsafe"

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
        # NormalizePath: a Windows verbatim prefix is stripped (\\?\C:\dir -> C:\dir).
        for prefix in ("\\\\?\\", "//?/"):
            if override.startswith(prefix):
                return override[len(prefix) :]
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


def _uses_local_zmq_ipc(env: Mapping[str, str], platform: str) -> bool:
    """``UseLocalZmqIpc``: on by default on macOS, ``CLIO_ZMQ_LOCAL_IPC`` overrides."""
    stated = env.get("CLIO_ZMQ_LOCAL_IPC")
    if stated is not None:
        return stated not in ("", "0")
    return platform == "darwin"


def ipc_socket_paths(env: Mapping[str, str], port: int, platform: str | None = None) -> list[str]:
    """Every Unix socket the daemon must listen on; it exits when any one fails.

    On macOS the local client ROUTER also runs over ``clio_zmq_<port+3>.ipc``
    (clio-core ``LocalZmqIpcPath``), four bytes longer than the main socket.
    """
    name = platform or sys.platform
    paths = [ipc_socket_path(env, port, name)]
    if _uses_local_zmq_ipc(env, name):
        paths.append(f"{memfd_dir(env, name)}/clio_zmq_{port + 3}.ipc")
    return paths


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


class ClioCoreMemfdDirUnsafe(DaemonSpawnFailed):
    """The per-user directory is not this user's private directory; the daemon exits 1."""

    degradation_reason = CLIO_CORE_MEMFD_DIR_UNSAFE

    def __init__(self, path: str, why: str) -> None:
        self.path = path
        super().__init__(
            f"clio-core cannot start (reason={CLIO_CORE_MEMFD_DIR_UNSAFE}): {path!r} {why}. "
            "clio-core keeps its IPC socket and shared-memory links there and, from "
            "iowarp-core 2.5, uses it only when it is a real directory owned by this user "
            "that no one else can write. Remove it, fix its owner or mode (0700), or point "
            "CLIO_MEMFD_DIR at a private directory."
        )


def _memfd_dir_problem(path: str) -> str | None:
    """Why clio-core's ``CheckMemfdDir`` would call ``path`` unsafe (POSIX), or None."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return None  # the daemon creates it (mode 0700)
    except OSError as exc:
        return f"cannot be inspected ({exc.strerror})"
    if stat.S_ISLNK(st.st_mode):
        return "is a symbolic link"
    if not stat.S_ISDIR(st.st_mode):
        return "is not a directory"
    if st.st_uid != os.geteuid():  # type: ignore[attr-defined]  # POSIX only
        return f"belongs to uid {st.st_uid}, not this user"
    if st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        return f"is writable by group or others (mode {stat.S_IMODE(st.st_mode):o})"
    return None


def require_ipc_path_fits(env: Mapping[str, str], port: int, platform: str | None = None) -> None:
    """Raise a typed spawn failure when the daemon could not bind its sockets.

    :class:`ClioCoreIpcPathTooLong` for a socket path over ``sun_path``, and on
    POSIX :class:`ClioCoreMemfdDirUnsafe` for a directory clio-core would refuse.
    """
    capacity = sun_path_capacity(platform)
    for path in ipc_socket_paths(env, port, platform):
        if len(path.encode("utf-8")) >= capacity:
            raise ClioCoreIpcPathTooLong(path, capacity)
    if platform is None and not sys.platform.startswith("win"):
        require_private_memfd_dir(env)


def require_private_memfd_dir(env: Mapping[str, str]) -> None:
    """Raise :class:`ClioCoreMemfdDirUnsafe` when clio-core would refuse the dir (POSIX)."""
    directory = memfd_dir(env)
    problem = _memfd_dir_problem(directory)
    if problem is not None:
        raise ClioCoreMemfdDirUnsafe(directory, problem)
