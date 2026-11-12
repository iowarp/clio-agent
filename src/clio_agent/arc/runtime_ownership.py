"""Which clio-core daemon a runtime state directory owns (owner module).

The daemon's RPC port is a machine-wide resource, but its bookkeeping (spawn lock,
pidfile, client registry, version record) lives in a runtime state directory under
each user's home (:func:`clio_agent.arc.clio_core_config.runtime_state_dir`). Two
homes on one machine (two users, or one user running with two ``HOME`` values) used
to meet on the default port 9413. The second attached to the first's daemon, adopting
its storage, and when its own client registry emptied, stopped it. Users never share
a daemon, so the rules here are:

* A listener is this state directory's daemon only when the state directory's pidfile
  names a live process (PID-reuse checked) and that process, or a descendant, holds
  the listening socket. When the operating system will not say who listens, the live
  pidfile decides.
* A listener on the configured port that this state directory does not own is
  foreign: it is never attached to and never stopped. Connect-or-spawn moves this
  state directory's daemon to a free port block and records that port in the
  generated ``cte.yaml``, so the move holds across restarts. A hand-written config
  is not rewritten; the attach refuses with a typed reason that names the setting.

The rules are the same on Linux, macOS and Windows.
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
import socket
import sys
import zlib
from pathlib import Path
from typing import Literal

from clio_agent.errors import ClioError

logger = logging.getLogger(__name__)

#: Typed reasons (no silent fallback).
DAEMON_NOT_OWNED = "clio_core_daemon_not_owned"
FOREIGN_DAEMON_ON_PORT = "clio_core_foreign_daemon_on_port"
PORT_MOVED = "clio_core_port_moved_off_foreign_daemon"

#: clio-core binds a small contiguous block from its base port (the 65530 ceiling of
#: ``arc.core_port`` leaves room for it); a candidate base needs the whole block free.
_PORT_BLOCK = 6
_CANDIDATE_FIRST = 9421
_CANDIDATE_STEP = 8
_CANDIDATE_SLOTS = 512

Ownership = Literal["owned", "starting", "foreign", "none"]

_NETWORKING_PORT = re.compile(
    r"(?m)^(networking:[ \t]*\r?\n(?:[ \t]+[^\r\n]*\r?\n)*?[ \t]+port:[ \t]*)(\d+)"
)


class ClioCoreForeignDaemonError(ClioError):
    """Another user's or home's clio-core daemon holds this config's port."""

    degradation_reason = FOREIGN_DAEMON_ON_PORT

    def __init__(self, message: str) -> None:
        super().__init__(message, error_type=FOREIGN_DAEMON_ON_PORT)


def listener_belongs_to(pid: int, port: int) -> bool | None:
    """Whether ``pid`` or a descendant listens on TCP ``port`` (``None``: unreadable)."""
    import psutil  # noqa: PLC0415

    try:
        root = psutil.Process(pid)
        processes = [root, *root.children(recursive=True)]
    except psutil.NoSuchProcess:
        return False
    except psutil.AccessDenied:
        return None
    unreadable = False
    for process in processes:
        try:
            connections = process.net_connections(kind="tcp")
        except psutil.NoSuchProcess:
            continue
        except psutil.AccessDenied:
            unreadable = True
            continue
        for connection in connections:
            if (
                connection.status == psutil.CONN_LISTEN
                and connection.laddr
                and connection.laddr.port == port
            ):
                return True
    return None if unreadable else False


def owned_daemon_pid() -> int | None:
    """The live daemon this state directory's pidfile names, or ``None``."""
    from clio_agent.arc import storage  # noqa: PLC0415 - storage imports this module

    return storage.pidfile_live_pid(storage._daemon_pidfile())


def daemon_ownership(port: int, *, listening: bool) -> Ownership:
    """Classify the daemon on ``port`` relative to this state directory.

    ``owned``: our pidfile daemon holds the listener (or the OS will not say who does).
    ``starting``: our pidfile daemon is alive but nothing listens yet.
    ``foreign``: something listens that our pidfile daemon does not hold.
    ``none``: nothing listens and no daemon of ours runs.
    """
    pid = owned_daemon_pid()
    if pid is None:
        return "foreign" if listening else "none"
    if not listening:
        return "starting"
    return "foreign" if listener_belongs_to(pid, port) is False else "owned"


def stop_guard(port: int) -> Literal["owned", "not_owned", "foreign_listener"]:
    """Whether a stop of the daemon on ``port`` may run, and what it may touch.

    ``clio_run stop`` stops whatever daemon serves the config's port, so it may run
    only when this state directory's live daemon is the one listening (``owned``; also
    when that daemon has not bound yet, or the OS will not say who listens).
    ``not_owned``: no daemon of ours runs, so nothing is stopped.
    ``foreign_listener``: ours runs but another holds the port, so only ours is killed.
    """
    from clio_agent.arc.clio_core_liveness import (  # noqa: PLC0415 - cycle
        _runtime_alive,
        runtime_socket_status,
    )

    pid = owned_daemon_pid()
    if pid is None:
        return "not_owned"
    if listener_belongs_to(pid, port) is not False:
        return "owned"
    listening, reason = runtime_socket_status(port, probe=_runtime_alive)
    return "owned" if not listening and reason is None else "foreign_listener"


def _port_taken(port: int) -> bool:
    """Whether anything holds ``port`` on this machine (bind or connect says so)."""
    for host in ("127.0.0.1", "0.0.0.0"):  # noqa: S104 - a bind probe, closed at once
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            if sys.platform.startswith("win"):
                # Windows otherwise lets a specific bind shadow another's wildcard listener.
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            try:
                probe.bind((host, port))
            except OSError:
                return True
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.5)
        return probe.connect_ex(("127.0.0.1", port)) == 0


def _block_free(base: int) -> bool:
    return not any(_port_taken(port) for port in range(base, base + _PORT_BLOCK))


def private_port_candidates(state_dir: Path) -> list[int]:
    """Candidate base ports for ``state_dir``, starting at one derived from its path.

    Deriving the start from the state directory keeps two homes that relocate at the
    same moment (each under its own spawn lock) from probing the same block first.
    """
    start = zlib.crc32(str(state_dir).encode("utf-8")) % _CANDIDATE_SLOTS
    return [
        _CANDIDATE_FIRST + ((start + offset) % _CANDIDATE_SLOTS) * _CANDIDATE_STEP
        for offset in range(_CANDIDATE_SLOTS)
    ]


def generated_config(config_path: str) -> bool:
    """Whether ``config_path`` is CLIO's generated default ``cte.yaml`` (safe to rewrite)."""
    from clio_agent.arc.clio_core_config import _default_cte_dir  # noqa: PLC0415 - cycle

    if not config_path:
        return False
    with contextlib.suppress(OSError):
        return Path(config_path).resolve() == (_default_cte_dir() / "cte.yaml").resolve()
    return False


def _write_port(config: Path, port: int) -> None:
    text = config.read_text(encoding="utf-8")
    updated, count = _NETWORKING_PORT.subn(lambda match: f"{match.group(1)}{port}", text, 1)
    if count != 1:
        raise ClioCoreForeignDaemonError(
            f"{config} declares no networking.port to move; set arc.core_port to a free "
            "port and remove the file so CLIO generates it again."
        )
    staged = config.with_name(config.name + ".port-move")
    staged.write_text(updated, encoding="utf-8")
    os.replace(staged, config)
    from clio_agent.arc.clio_core_growth import rekey_managed_capacity  # noqa: PLC0415 - cycle

    rekey_managed_capacity(config, text, updated)


def move_off_foreign_daemon(config_path: str, foreign_port: int, state_dir: Path) -> int:
    """Give this state directory's generated config a free port block; return the port.

    Called under the spawn lock when a daemon this state directory does not own holds
    ``foreign_port``. Raises :class:`ClioCoreForeignDaemonError` for a hand-written
    config or when no free block exists, never attaching to the foreign daemon.
    """
    if not generated_config(config_path):
        raise ClioCoreForeignDaemonError(
            f"Another user's or home's clio-core daemon holds port {foreign_port}, which "
            f"{config_path or 'this config'} declares. CLIO never attaches to or stops a "
            "daemon it did not start. Set networking.port in that file (or arc.core_port "
            "for a generated config) to a free port."
        )
    for base in private_port_candidates(state_dir):
        if _block_free(base):
            _write_port(Path(config_path), base)
            logger.warning(
                "clio-core port %d is held by a daemon this home did not start; this "
                "home's daemon now uses port %d (reason=%s config=%s)",
                foreign_port,
                base,
                PORT_MOVED,
                config_path,
            )
            return base
    raise ClioCoreForeignDaemonError(
        f"Another user's or home's clio-core daemon holds port {foreign_port} and no free "
        f"port block was found from {_CANDIDATE_FIRST}; set arc.core_port to a free port."
    )
