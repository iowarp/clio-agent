"""PID-reuse-safe process identity for the clio-core client registry (owner module).

Moved out of ``arc/storage.py`` (file-size ratchet, #774): the two pure psutil helpers
the client registry and the daemon pidfile use to tell a live process from a recycled
PID. ``storage`` re-exports them as ``_proc_create_time`` / ``_pid_alive`` so existing
callers (``runtime_stop``, ``clio_core_daemon``, ``runtime/status``, tests) and their
monkeypatch seams are unchanged.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


def proc_create_time(pid: int) -> float | None:
    """Process creation time (epoch seconds) via psutil, or None if no such process.

    Used to defeat PID reuse: a recycled PID gets a different creation time, so a stale
    registry entry won't be mistaken for a live client. psutil makes this portable
    across Linux/macOS/Windows (no ``/proc`` dependency).
    """
    try:
        import psutil  # noqa: PLC0415

        return float(psutil.Process(pid).create_time())
    except Exception:  # noqa: BLE001 - NoSuchProcess/AccessDenied/import => "unknown"
        return None


def pid_alive(pid: int, recorded_create_time: float | None) -> bool:
    """True if ``pid`` is alive AND (when known) its creation time matches the record.

    Matching creation time within ~1s tolerance defeats PID reuse; when the recorded
    value is absent we fall back to bare existence.
    """
    try:
        import psutil  # noqa: PLC0415

        if not psutil.pid_exists(pid):
            return False
    except Exception:  # noqa: BLE001 - psutil missing => treat as conservatively alive
        return proc_create_time(pid) is not None
    if recorded_create_time is None:
        return True  # creation time wasn't captured; bare existence is enough
    current = proc_create_time(pid)
    return current is not None and abs(current - recorded_create_time) < 1.0


def pidfile_live_pid(pidfile: "Path") -> int | None:
    """The live process a ``<pid> <create_time>`` pidfile names, or ``None``.

    ``None`` for a missing/unreadable/malformed pidfile and for a recorded process that
    exited (or whose PID was reused), so a caller never acts on a stale record.
    """
    try:
        parts = pidfile.read_text(encoding="utf-8").split()
        pid = int(parts[0])
        recorded = float(parts[1]) if len(parts) > 1 and parts[1] else None
    except (OSError, ValueError, IndexError):
        return None
    return pid if pid_alive(pid, recorded) else None
