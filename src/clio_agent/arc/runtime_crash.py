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
from pathlib import Path
from typing import Any, Protocol

logger = logging.getLogger(__name__)

CRASH_RECORD_NAME = "clio-runtime-crash.json"

#: Daemon-log lines carried into the record — enough to name the failure without
#: unbounded payloads in error details.
_LOG_TAIL_LINES = 30


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


def _log_tail(log_path: Path) -> str:
    try:
        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    return "\n".join(lines[-_LOG_TAIL_LINES:])


def _exit_code_hex(exit_code: int) -> str:
    """Windows NTSTATUS view of a negative exit code (0xC0000005-style)."""

    return f"0x{exit_code & 0xFFFFFFFF:08X}"


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
) -> threading.Thread:
    """Watch the spawned daemon; write a typed crash record on abnormal exit.

    Returns the (daemon) watcher thread so tests can join it. A clean exit
    (rc == 0) writes nothing — stopping the daemon is not a crash.
    """

    def _watch() -> None:
        exit_code = proc.wait()
        if exit_code == 0:
            return
        record = {
            "pid": proc.pid,
            "exit_code": exit_code,
            "exit_code_hex": _exit_code_hex(exit_code),
            "crashed_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
            "log_tail": _log_tail(Path(log_path)),
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
