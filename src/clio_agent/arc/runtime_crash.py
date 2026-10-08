"""Typed crash records for the spawned clio-core runtime daemon (#1148).

The daemon must die LOUDLY in our own channels — never a desktop dialog, never a
vague "not listening". :func:`watch_daemon_process` puts a watcher thread on the
spawned daemon; an abnormal exit writes a typed JSON record (exit status, UTC
timestamp, daemon-log tail) into the runtime state dir, and the liveness gate
(:mod:`clio_agent.arc.clio_core_liveness`) enriches its
``ClioCoreRuntimeLostError`` from that record so the failure NAMES the crash at
the exact point tests and traces look. Nothing here suppresses the OS-level
crash reporting; this only adds visibility on our side.

The watcher lives in the spawning process, so a record is written whenever the
spawner outlives the daemon — the test-suite and server cases, which are exactly
where crashes were being misread as environment flakes. A daemon that outlives
its spawner still surfaces through the (now record-less) liveness error.
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

logger = logging.getLogger(__name__)

CRASH_RECORD_NAME = "clio-runtime-crash.json"

#: Daemon-log lines carried into the record — enough to name the failure without
#: unbounded payloads in error details.
_LOG_TAIL_LINES = 30

# ``clio_run stop`` terminates the daemon with a non-zero platform status on
# Windows (observed as 15).  The watcher cannot infer intent from that status
# alone: the same status could also come from an external termination.  Record
# the lifecycle-owned stop request before invoking the launcher so only that
# exact daemon exit is treated as expected.
_expected_exit_pids: set[int] = set()
_expected_exit_lock = threading.Lock()


class _WaitableProcess(Protocol):
    """The slice of ``subprocess.Popen`` the watcher needs (tests use real Popen)."""

    pid: int

    def wait(self) -> int:  # pragma: no cover - Protocol signature
        ...


def crash_record_path(state_dir: Path) -> Path:
    """The typed crash-record location under ``state_dir``."""

    return Path(state_dir) / CRASH_RECORD_NAME


def clear_crash_record(state_dir: Path) -> None:
    """Drop a stale record so a fresh spawn starts with a clean slate."""

    try:
        crash_record_path(state_dir).unlink(missing_ok=True)
    except OSError as exc:
        logger.warning("could not clear stale daemon crash record: %r", exc)


def read_crash_record(state_dir: Path) -> dict[str, Any] | None:
    """Return the typed crash record if one exists and parses, else ``None``.

    An unreadable/corrupt record is surfaced as a warning (not silently ignored)
    and treated as absent — the liveness error then falls back to its base
    message, which is still typed and quarantining.
    """

    path = crash_record_path(state_dir)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        logger.warning("daemon crash record unreadable at %s: %r", path, exc)
        return None
    try:
        record = json.loads(raw)
    except ValueError as exc:
        logger.warning("daemon crash record corrupt at %s: %r", path, exc)
        return None
    return record if isinstance(record, dict) else None


def _log_tail(log_path: Path, offset: int = 0) -> str:
    """Last lines this daemon wrote; the log is shared and appended across spawns."""
    try:
        with log_path.open("rb") as fh:
            fh.seek(offset)
            lines = fh.read().decode("utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    return "\n".join(lines[-_LOG_TAIL_LINES:])


def _exit_code_hex(exit_code: int) -> str:
    """Windows NTSTATUS view of a negative exit code (0xC0000005-style)."""

    return f"0x{exit_code & 0xFFFFFFFF:08X}"


def expect_daemon_exit(pid: int) -> None:
    """Mark ``pid`` as intentionally stopping through the managed lifecycle."""

    with _expected_exit_lock:
        _expected_exit_pids.add(pid)


def _consume_expected_exit(pid: int) -> bool:
    """Return and clear whether ``pid`` has a lifecycle-owned stop request."""

    with _expected_exit_lock:
        if pid not in _expected_exit_pids:
            return False
        _expected_exit_pids.remove(pid)
        return True


def summarize_crash(record: dict[str, Any]) -> str:
    """One-line human summary used inside liveness error messages."""

    status = record.get("exit_code_hex") or record.get("exit_code")
    tail = (record.get("log_tail") or "").strip()
    last_line = tail.splitlines()[-1] if tail else "(no daemon log output)"
    return (
        f"the daemon (pid {record.get('pid')}) CRASHED with exit status {status} "
        f"at {record.get('crashed_at')}; last log line: {last_line!r}; "
        f"full log: {record.get('log_path')}"
    )


def watch_daemon_process(
    proc: _WaitableProcess,
    *,
    log_path: Path,
    state_dir: Path,
    log_offset: int = 0,
) -> threading.Thread:
    """Watch the spawned daemon; write a typed crash record on abnormal exit.

    Returns the (daemon) watcher thread so tests can join it. A clean exit
    (rc == 0) writes nothing — stopping the daemon is not a crash.
    ``log_offset`` is the log size at spawn, so the tail never shows an earlier
    daemon's output as this one's last words (F019).
    """

    def _watch() -> None:
        exit_code = proc.wait()
        if exit_code == 0 or _consume_expected_exit(proc.pid):
            return
        record = {
            "pid": proc.pid,
            "exit_code": exit_code,
            "exit_code_hex": _exit_code_hex(exit_code),
            "crashed_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
            "log_tail": _log_tail(Path(log_path), log_offset),
            "log_path": str(log_path),
        }
        try:
            with crash_record_path(Path(state_dir)).open("w", encoding="utf-8") as fh:
                json.dump(record, fh, indent=2)
        except OSError as exc:
            logger.error("could not write daemon crash record: %r", exc)
        logger.error(
            "clio-core runtime daemon crashed: pid=%s exit=%s (%s); log: %s",
            proc.pid,
            exit_code,
            record["exit_code_hex"],
            log_path,
        )

    thread = threading.Thread(target=_watch, name="clio-runtime-crash-watcher", daemon=True)
    thread.start()
    return thread


class DaemonSpawnFailed(RuntimeError):
    """A spawned daemon crashed or stopped progressing before it bound its RPC port."""

    degradation_reason = "clio_core_daemon_spawn_failed"


class DaemonStillStarting(DaemonSpawnFailed):
    """A spawned daemon was still visibly starting at the ``arc.liveness.max_wait_s`` ceiling.

    Never killed: it is left to finish and the next attach adopts it.
    """

    degradation_reason = "clio_core_daemon_start_ceiling"


def daemon_start_work(pid: int | None, log_path: Path) -> Callable[[], float | None]:
    """The startup progress signal of a spawned daemon: its process tree's CPU + I/O work
    plus its log's growth (MiB). ``None`` when there is no daemon process to measure."""
    from clio_agent.runtime.progress import ProcessTreeWork  # noqa: PLC0415

    tree = ProcessTreeWork(pid) if pid is not None else None

    def sample() -> float | None:
        work = tree.sample() if tree is not None else None
        if work is None:
            return None
        try:
            log_mib = log_path.stat().st_size / float(1 << 20)
        except OSError:
            log_mib = 0.0
        return work + log_mib

    return sample


def wait_for_spawned_daemon(
    port: int,
    *,
    alive: Callable[[int], bool],
    state_dir: Path,
    work: Callable[[], float | None],
    no_progress_s: float,
    ceiling_s: float | None = None,
    poll_s: float = 0.25,
) -> None:
    """Wait until a just-spawned daemon binds ``port`` while its startup makes progress.

    No fixed deadline: a slow machine starts the daemon slowly, and a daemon still
    working (``work`` advancing: its CPU/I/O, its log growing) is waited for. The
    watcher (:func:`watch_daemon_process`) writes the crash record the moment the
    daemon exits, so a crash fails at once with the daemon's own exit status and last
    log line.

    Args:
        port: The RPC port the daemon must bind.
        alive: Liveness probe for ``port``.
        state_dir: The runtime state dir holding the crash record and log.
        work: The startup progress signal (:func:`daemon_start_work`).
        no_progress_s: A whole window this long with no progress is a failed start.
        ceiling_s: Bound on waiting for a daemon still progressing
            (``arc.liveness.max_wait_s`` when ``None``).
        poll_s: Poll interval.

    Raises:
        DaemonSpawnFailed: The daemon crashed, or made no progress for ``no_progress_s``.
        DaemonStillStarting: The daemon was still progressing at the ceiling.
    """
    from clio_agent.arc.daemon_progress import (  # noqa: PLC0415 - cycle
        CEILING,
        DONE,
        wait_while_progressing,
    )

    crashed: list[dict[str, Any]] = []

    def bound_within(timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while True:
            if alive(port):
                return True
            record = read_crash_record(state_dir)
            if record is not None:
                crashed.append(record)
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(poll_s)

    outcome = wait_while_progressing(
        bound_within,
        slice_s=no_progress_s,
        op_name="daemon_start",
        work=work,
        ceiling_s=ceiling_s,
    )
    if crashed:
        raise DaemonSpawnFailed(
            "spawned the clio-core runtime daemon but it exited before binding port "
            f"{port}: {summarize_crash(crashed[0])}"
        )
    if outcome.reason == DONE:
        return
    log = state_dir / "clio-runtime.log"
    if outcome.reason == CEILING:
        raise DaemonStillStarting(
            f"the clio-core runtime daemon is still starting after {outcome.waited_s:.0f}s "
            f"(port {port} not bound yet; ceiling arc.liveness.max_wait_s); it is left "
            f"running for the next attach to adopt; see {log}."
        )
    raise DaemonSpawnFailed(
        f"spawned the clio-core runtime daemon but it never bound port {port} and made no "
        f"progress for {no_progress_s:g}s (wait={outcome.reason}); see {log}."
    )
