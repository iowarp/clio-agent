"""A clio-core daemon whose every client crashed is replaced, never adopted (owner module).

The shared daemon counts its clients in a registry (one file per client PID, see
:mod:`clio_agent.arc.storage`). A client that exits cleanly removes its file; a file
whose process is gone therefore names a client that CRASHED (SIGKILL, OOM, power cut)
while attached.

Such a crash can leave the daemon half-alive. clio-core 2.3.x receives a client's
requests on a shared-memory ring, one chunk of at most 32 KiB at a time, and keeps a
partly received message per connection until its last chunk arrives. A client killed
between two chunks of one message never sends the rest, and the daemon worker that
drains the ring then spins on that half message for good (``RecvBytes`` never reports
the ring empty while a message is half received, so the worker never returns to its
own queue). A client killed between reserving a ring slot and stamping its pid on
it is worse: while the slot still carries no producer pid (the first lap of the
ring) the worker waits on it forever and nothing behind it is ever read. Either way
the TCP port still accepts connections, so the next client "finds a daemon", adopts
it, and hangs: the native handshake is never answered, and a request that does land
never completes. Both reproduced deterministically by a stand-in producer that writes
the ring state and exits (``put`` then waits out every stall ceiling; a new client's
attach fails after 180 s). The daemon's busy spin also reads as progress to the
CPU-based waits, so every bounded wait ran to its ceiling.

A daemon no live client uses holds nothing a fresh one lacks: every acknowledged write
is already committed to the store's file tier (the durability rule ``crash_test.py
--kill-core`` checks). So connect-or-spawn stops it -- a clean ``clio_run stop``,
bounded, with the pidfile kill behind it -- and spawns a fresh daemon on the same
store, typed ``clio_core_daemon_replaced_after_client_crash`` in the log and as the
``clio_core_daemon_replaced`` row of ``/v1/health``. A daemon another live client still uses is
left alone (that client owns it).
"""

from __future__ import annotations

import contextlib
import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

#: Typed reason: the daemon outlived every client it served (each died without
#: releasing it), so it was stopped and a fresh one spawned instead of adopted.
DAEMON_REPLACED_AFTER_CLIENT_CRASH = "clio_core_daemon_replaced_after_client_crash"
#: Typed reason: the replaced daemon's process did not exit after the stop.
DAEMON_REPLACEMENT_EXIT_UNCONFIRMED = "clio_core_daemon_replacement_exit_unconfirmed"

# How long to wait for the stopped daemon's process to be gone before spawning anew.
_EXIT_WAIT_S = 10.0

_replacement: dict[str, Any] = {}


def client_census(*, prune: bool = True) -> tuple[list[int], list[int]]:
    """``(live, crashed)`` client PIDs of the registry; crashed entries pruned if ``prune``.

    A crashed entry is a file whose process is gone or whose PID was reused (its
    recorded start time differs); an unreadable record counts as unknown start time.
    """
    from clio_agent.arc import storage  # noqa: PLC0415 - storage imports this module

    reg = storage._client_registry_dir()
    if not reg.is_dir():
        return [], []
    live: list[int] = []
    crashed: list[int] = []
    for entry in reg.iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        try:
            raw = entry.read_text(encoding="utf-8").strip()
            recorded: float | None = float(raw) if raw else None
        except (OSError, ValueError):
            recorded = None
        if storage._pid_alive(pid, recorded):
            live.append(pid)
        else:
            crashed.append(pid)
            if prune:
                _prune(pid)
    return live, crashed


def _prune(pid: int) -> None:
    from clio_agent.arc import storage  # noqa: PLC0415 - storage imports this module

    with contextlib.suppress(OSError):
        (storage._client_registry_dir() / str(pid)).unlink()


def replace_daemon_left_by_crashed_clients(config_path: str, log_level: str) -> bool:
    """Stop the daemon when every client it served crashed; ``True`` when one was stopped.

    Called by connect-or-spawn with the host-global spawn lock held, before this
    process registers, so no client can attach to the daemon while it is stopped.
    Nothing happens when a live client remains, when no client crashed, or when no
    daemon is running. The stop is bounded (``arc.liveness.stop_no_progress_s``, not the
    daemon's CPU, which a wedged daemon keeps busy) and then waits for the process to
    exit, so the caller spawns on a free port.

    Args:
        config_path: The config this process asked for; the running daemon's own
            config (its port) is resolved from the daemon record.
        log_level: Native log level for the stop helper.
    """
    from clio_agent.arc import clio_core_daemon_version as daemon_version  # noqa: PLC0415
    from clio_agent.arc import runtime_stop, storage  # noqa: PLC0415 - import cycle
    from clio_agent.arc.clio_core_config import runtime_state_dir  # noqa: PLC0415
    from clio_agent.arc.clio_core_liveness import ClioCoreRuntimeLostError  # noqa: PLC0415

    # Crashed entries are pruned only once the daemon is stopped: a start killed during
    # the stop leaves them, so the next start still knows the daemon is suspect.
    live, crashed = client_census(prune=False)
    if live or not crashed:
        return False
    running = daemon_version.running_daemon_config(runtime_state_dir(), config_path)
    daemon_pid = storage.pidfile_live_pid(storage._daemon_pidfile())
    try:
        listening = storage._runtime_alive(storage._resolve_runtime_port(running))
    except ClioCoreRuntimeLostError:
        listening = daemon_pid is not None  # inconclusive probe: trust the pidfile
    if daemon_pid is None and not listening:
        _prune_all(crashed)
        return False
    started = time.monotonic()
    logger.warning(
        "clio-core daemon pid=%s outlived its clients, which all exited without releasing "
        "it (crashed client pids=%s); stopping it and starting a fresh one instead of "
        "adopting it (reason=%s)",
        daemon_pid,
        sorted(crashed),
        DAEMON_REPLACED_AFTER_CLIENT_CRASH,
    )
    outcome = runtime_stop.stop_runtime_daemon(
        running, log_level, ceiling_s=runtime_stop.stop_no_progress_s()
    )
    exited = _wait_for_exit(daemon_pid)
    if exited:
        _prune_all(crashed)
    record = {
        "reason": DAEMON_REPLACED_AFTER_CLIENT_CRASH,
        "daemon_pid": daemon_pid,
        "crashed_clients": sorted(crashed),
        "stop_path": outcome.path,
        "exited": exited,
        "seconds": round(time.monotonic() - started, 2),
        "at": time.time(),
    }
    _replacement.clear()
    _replacement.update(record)
    if not exited:
        logger.error(
            "replaced clio-core daemon pid=%s did not exit after the stop (reason=%s)",
            daemon_pid,
            DAEMON_REPLACEMENT_EXIT_UNCONFIRMED,
        )
    logger.warning("clio-core daemon replaced after client crash: %s", record)
    return True


def _prune_all(pids: list[int]) -> None:
    for pid in pids:
        _prune(pid)


def _wait_for_exit(pid: int | None) -> bool:
    """Wait (bounded) for the stopped daemon's process to be gone; ``True`` once it is."""
    if pid is None:
        return True
    import psutil  # noqa: PLC0415

    try:
        proc = psutil.Process(pid)
        proc.wait(timeout=_EXIT_WAIT_S)
    except psutil.NoSuchProcess:
        return True
    except psutil.TimeoutExpired:
        try:
            return proc.status() == psutil.STATUS_ZOMBIE
        except psutil.NoSuchProcess:
            return True
    except psutil.Error:
        return False
    return True


def daemon_replacement() -> dict[str, Any]:
    """What :func:`replace_daemon_left_by_crashed_clients` did in this process ({} if none)."""
    return dict(_replacement)


def reset_daemon_replacement() -> None:
    """Forget the recorded replacement (test seam)."""
    _replacement.clear()
