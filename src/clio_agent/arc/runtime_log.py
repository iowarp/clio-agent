"""Bounded clio-core daemon log: spawn-time rotation and the log pump (owner module).

The detached daemon's stdout/stderr used to be an append-mode ``clio-runtime.log``
with no cap; a clio-core error path that logs on every iteration of an endless
loop grew it to 128 GB and filled a machine's disk. Two layers now bound it:

* :func:`open_daemon_log_sink` gives the daemon the write end of a pipe whose
  reader is :mod:`clio_agent.arc.runtime_log_pump` (collapses repeats, caps the
  file with one rotation, writes a flood record);
* when the pump cannot start, the daemon falls back to the raw file, and every
  spawn first rotates an oversized log (:func:`rotate_if_oversized`), so even the
  fallback is bounded across restarts.
"""

from __future__ import annotations

import contextlib
import logging
import os
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Union

from clio_agent.arc.runtime_spawn import _detached_popen_kwargs

logger = logging.getLogger(__name__)

DEFAULT_LOG_MAX_BYTES = 16 * 1024 * 1024
DEFAULT_FLOOD_LINES_PER_S = 1000
FLOOD_RECORD_NAME = "clio-runtime-flood.json"
#: Logged when the pump cannot start and the daemon writes the raw (spawn-capped) file.
LOG_PUMP_UNAVAILABLE = "clio_core_log_pump_unavailable"
_PUMP_SCRIPT = Path(__file__).with_name("runtime_log_pump.py")


def flood_record_path(state_dir: Path) -> Path:
    """Where the pump records an active log flood for ``state_dir``'s daemon."""
    return state_dir / FLOOD_RECORD_NAME


def resolve_log_max_bytes() -> int:
    """``arc.clio_core.log_max_bytes`` / ``CLIO_ARC_CLIO_CORE_LOG_MAX_BYTES`` (16 MiB).

    A malformed or non-positive value never lifts the cap: it falls back to the default.
    """
    from clio_agent import conf  # noqa: PLC0415 - avoid import cycle at module load

    try:
        value = conf.resolve(
            "arc.clio_core.log_max_bytes",
            env="CLIO_ARC_CLIO_CORE_LOG_MAX_BYTES",
            default=DEFAULT_LOG_MAX_BYTES,
            cast=conf.as_int,
        )
    except (TypeError, ValueError) as exc:
        logger.warning(
            "ignoring unparseable CLIO_ARC_CLIO_CORE_LOG_MAX_BYTES (reason=%s); using %d",
            exc,
            DEFAULT_LOG_MAX_BYTES,
        )
        return DEFAULT_LOG_MAX_BYTES
    if value <= 0:
        logger.warning(
            "refusing non-positive CLIO_ARC_CLIO_CORE_LOG_MAX_BYTES=%d; using %d",
            value,
            DEFAULT_LOG_MAX_BYTES,
        )
        return DEFAULT_LOG_MAX_BYTES
    return value


def resolve_flood_lines_per_s() -> int:
    """``arc.clio_core.log_flood_lines_per_s`` / ``CLIO_ARC_CLIO_CORE_LOG_FLOOD_LINES_PER_S``."""
    from clio_agent import conf  # noqa: PLC0415 - avoid import cycle at module load

    try:
        value = conf.resolve(
            "arc.clio_core.log_flood_lines_per_s",
            env="CLIO_ARC_CLIO_CORE_LOG_FLOOD_LINES_PER_S",
            default=DEFAULT_FLOOD_LINES_PER_S,
            cast=conf.as_int,
        )
    except (TypeError, ValueError) as exc:
        logger.warning(
            "ignoring unparseable CLIO_ARC_CLIO_CORE_LOG_FLOOD_LINES_PER_S (reason=%s); using %d",
            exc,
            DEFAULT_FLOOD_LINES_PER_S,
        )
        return DEFAULT_FLOOD_LINES_PER_S
    return value if value > 0 else DEFAULT_FLOOD_LINES_PER_S


def rotate_if_oversized(log_path: Path, max_bytes: int) -> bool:
    """Move ``log_path`` to ``<log>.1`` when it already exceeds ``max_bytes``.

    Returns ``True`` when it rotated. Any earlier ``.1`` is replaced, so at most one
    capped generation is kept. An OS error is logged and the spawn continues.
    """
    try:
        size = log_path.stat().st_size
    except FileNotFoundError:
        return False
    except OSError as exc:
        logger.warning("could not stat clio-core daemon log %s: %r", log_path, exc)
        return False
    if size <= max_bytes:
        return False
    try:
        os.replace(log_path, log_path.with_name(log_path.name + ".1"))
    except OSError as exc:
        logger.warning("could not rotate oversized clio-core daemon log %s: %r", log_path, exc)
        return False
    logger.warning(
        "rotated oversized clio-core daemon log %s (%d bytes > cap %d) to %s.1",
        log_path,
        size,
        max_bytes,
        log_path.name,
    )
    return True


@dataclass
class DaemonLogSink:
    """What the daemon's stdout/stderr point at: a pipe write fd or the raw log file."""

    target: Union[int, IO[bytes]]
    pump_pid: int | None = None

    def close(self) -> None:
        """Release the parent's handle once the daemon holds its own copy."""
        if isinstance(self.target, int):
            with contextlib.suppress(OSError):
                os.close(self.target)
        else:
            self.target.close()


def _pump_argv(log_path: Path, max_bytes: int, state_dir: Path, pidfile: Path) -> list[str]:
    return [
        sys.executable,
        "-I",
        "-B",
        str(_PUMP_SCRIPT),
        "--log",
        str(log_path),
        "--max-bytes",
        str(max_bytes),
        "--flood-record",
        str(flood_record_path(state_dir)),
        "--pidfile",
        str(pidfile),
        "--flood-lines-per-s",
        str(resolve_flood_lines_per_s()),
    ]


def start_log_pump(
    log_path: Path, max_bytes: int, state_dir: Path, pidfile: Path
) -> DaemonLogSink | None:
    """Start the detached pump; return the pipe write end for the daemon, or None.

    The pump is detached like the daemon (it must outlive the server) and exits
    at EOF, i.e. when the daemon exits. If the daemon is never spawned, closing the
    returned sink gives the pump EOF at once.
    """
    if not sys.executable or not _PUMP_SCRIPT.is_file():
        return None
    read_fd, write_fd = os.pipe()
    argv = _pump_argv(log_path, max_bytes, state_dir, pidfile)

    def _spawn(*, breakaway: bool) -> "subprocess.Popen[bytes]":
        return subprocess.Popen(  # type: ignore[call-overload]  # noqa: S603 - fixed argv
            argv,
            stdin=read_fd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            **_detached_popen_kwargs(breakaway=breakaway),
        )

    try:
        try:
            proc = _spawn(breakaway=True)
        except PermissionError:
            if not sys.platform.startswith("win"):
                raise
            proc = _spawn(breakaway=False)  # same Job Object downgrade as the daemon
    except (OSError, ValueError) as exc:
        os.close(write_fd)
        logger.warning(
            "clio-core log pump did not start (reason=%s: %r); the daemon writes %s "
            "directly, capped only at each spawn",
            LOG_PUMP_UNAVAILABLE,
            exc,
            log_path,
        )
        return None
    finally:
        os.close(read_fd)
    # Reap the pump when it exits at EOF, so a long-lived server (or the test suite)
    # does not keep one zombie per daemon spawn. Daemon thread: never blocks exit.
    threading.Thread(target=proc.wait, name="clio-runtime-log-pump-reaper", daemon=True).start()
    return DaemonLogSink(target=write_fd, pump_pid=proc.pid)


def open_daemon_log_sink(log_path: Path, state_dir: Path, pidfile: Path) -> DaemonLogSink:
    """Prepare the daemon's output target: rotate an oversized log, then pump or fall back."""
    max_bytes = resolve_log_max_bytes()
    rotate_if_oversized(log_path, max_bytes)
    with contextlib.suppress(OSError):
        flood_record_path(state_dir).unlink()  # a fresh daemon has no flood yet
    sink = start_log_pump(log_path, max_bytes, state_dir, pidfile)
    if sink is not None:
        return sink
    return DaemonLogSink(target=open(log_path, "ab"))  # noqa: SIM115 - handed to the child
