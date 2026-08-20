"""Liveness-driven wait around the shared uvx/uv-run launcher cache (#1237 hotfix).

``mcp_config.py::transport_for`` isolates every clio-spawned ``uvx``/``uv run``
stdio MCP launcher onto ONE dedicated cache dir
(``mcp_config._mcp_uv_cache_dir``) so clio's spawns never race the
developer's ambient uv cache. That isolation does not, by itself, stop
clio's OWN concurrent cold-cache spawns from racing EACH OTHER on that
shared dedicated dir — the exact failure ``transport_for``'s docstring
already documents: concurrent cold-cache ``uvx`` spawns building the same
ephemeral env archive can truncate ``pyvenv.cfg`` (astral-sh/uv#11694),
dropping the proxy connection and failing every tool-declaring expert.

Every stdio spawn onto the shared dedicated cache acquires a clio-owned file
lock (``filelock.FileLock``) before starting. The ORIGINAL (#1232 pt 3)
design raced that acquisition against a fixed ~15s deadline and failed FAST
+ typed on expiry. Real-world usage (iowarp/clio-agent#1237, an NFS-backed
home dir with concurrent cold spawns) proved that bound wrong: it raced a
LEGITIMATE holder's genuine work (a cold uv env build on a slow filesystem)
and dropped the server for the whole run with no retry, even though nothing
was actually broken.

Owner ruling (2026-08-20): **never race a deadline against a live holder.**
This module now waits on REALITY signals instead of a clock:

* While the lock's recorded holder PID names a LIVE process, keep waiting —
  no matter how long, because that holder is doing the shared cold-spawn
  work this caller also needs, and waiting is correct on any filesystem at
  any speed.
* When the recorded holder PID is confirmed DEAD, the lock is an abandoned
  artifact (a crash, or an NFS soft-lock the OS never actually enforced) —
  break it (typed, loud) and retry immediately.
* A GENEROUS runaway backstop (default 10 minutes, :func:`launcher_cache_lock_timeout_s`)
  exists ONLY to catch a livelocked holder or a wait this process could never
  identify a holder for — never the normal path, and it fires typed and loud
  (:data:`clio_agent.errors.LAUNCHER_CACHE_LOCK_TIMEOUT`).
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path

from filelock import FileLock, Timeout

from clio_agent.errors import LAUNCHER_CACHE_LOCK_STALE_BROKEN, LAUNCHER_CACHE_LOCK_TIMEOUT

logger = logging.getLogger(__name__)

#: Generous runaway backstop, never a normal-path bound (#1237). The OLD
#: default (15s) was a fail-fast contention bound; it is replaced by a value
#: large enough that only a genuinely livelocked/unidentifiable wait ever
#: hits it.
_DEFAULT_RUNAWAY_S = 600.0
_POLL_INTERVAL_S = 1.0
_LOCK_FILENAME = ".clio-launcher.lock"
_OWNER_SUFFIX = ".owner"


class LauncherCacheLockTimeoutError(RuntimeError):
    """The launcher-cache lock wait exceeded its generous runaway backstop.

    #1237: this is NEVER the normal path. A live holder is waited out
    regardless of duration (see :func:`acquire_launcher_cache_lock`); this
    only fires when the wait could not make forward progress by either
    reality signal (a livelocked holder, or one this process could never
    identify from the owner record) for the full backstop window.
    """

    def __init__(self, server_id: str, timeout_s: float) -> None:
        self.server_id = server_id
        self.timeout_s = timeout_s
        super().__init__(
            f"MCP server {server_id!r}: launcher cache lock wait exceeded its "
            f"{timeout_s:g}s runaway backstop (reason={LAUNCHER_CACHE_LOCK_TIMEOUT})"
        )


def launcher_cache_lock_timeout_s() -> float:
    """Generous runaway backstop (seconds) for the shared uv-launcher cache lock.

    #1237: NOT a normal-path bound — see the module docstring. Same config
    key/env var as the pre-#1237 fail-fast bound; the DEFAULT moved from 15s
    to a generous 600s because the old value raced legitimate slow-but-alive
    cold-spawn work on contended/NFS filesystems. An operator may still lower
    it, but the fix means the default alone is sufficient (no override
    required).
    """

    from clio_agent import conf  # noqa: PLC0415 - avoid import cycle at module load

    return float(
        conf.resolve(
            "tools.mcp.launcher_cache_lock_timeout_s",
            env="CLIO_MCP_LAUNCHER_CACHE_LOCK_TIMEOUT_S",
            default=_DEFAULT_RUNAWAY_S,
            cast=conf.as_float,
        )
    )


def _lock_path() -> Path:
    from clio_agent.tools.mcp_config import _mcp_uv_cache_dir  # noqa: PLC0415

    cache_dir = _mcp_uv_cache_dir()
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir / _LOCK_FILENAME


def _owner_path(lock_path: Path) -> Path:
    """The sidecar file recording the current holder's PID (#1237).

    Written by the holder INSIDE the critical section (after the OS-level
    lock is acquired) and cleared before release; a waiter reads it (best-
    effort — a missing/unreadable record just means "cannot identify the
    holder", never "the lock is free") to decide whether to keep waiting or
    to treat the lock as abandoned.
    """

    return lock_path.with_name(lock_path.name + _OWNER_SUFFIX)


def _read_owner_pid(owner_path: Path) -> int | None:
    """Best-effort read of the recorded holder PID (``None``: no/unreadable record)."""

    try:
        text = owner_path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def _write_owner_pid(owner_path: Path, pid: int) -> None:
    """Best-effort, near-atomic stamp of the current holder's PID (write-then-rename)."""

    tmp_path = owner_path.with_name(f"{owner_path.name}.{pid}.tmp")
    try:
        tmp_path.write_text(str(pid), encoding="utf-8")
        os.replace(tmp_path, owner_path)
    except OSError:
        with suppress(OSError):
            tmp_path.unlink(missing_ok=True)


def _clear_owner_pid(owner_path: Path) -> None:
    with suppress(OSError):
        owner_path.unlink(missing_ok=True)


def _pid_alive(pid: int) -> bool:
    """True when ``pid`` currently names a live LOCAL process (best-effort, #1237).

    Mirrors ``runtime.process_census._pid_alive`` (same psutil-gated check).
    Duplicated rather than imported: this is a low-level ``tools/`` primitive
    and must stay free of any ``runtime/`` import to avoid a load-order
    cycle risk. An unverifiable probe (no psutil, or a transient probe
    failure) NEVER counts as "dead" — that would break a live lock instead
    of correctly waiting on it.
    """

    try:
        import psutil  # noqa: PLC0415
    except ImportError:
        return True
    try:
        return bool(pid) and psutil.pid_exists(pid)
    except Exception:  # noqa: BLE001 - a probe failure is never "dead" evidence
        return True


def _break_stale_lock(lock_path: Path, owner_path: Path, server_id: str, holder_pid: int) -> None:
    """Remove an abandoned lock (#1237): its recorded holder PID is confirmed dead."""

    logger.warning(
        "launcher_cache_lock_stale_broken reason=%s server=%s holder_pid=%d",
        LAUNCHER_CACHE_LOCK_STALE_BROKEN,
        server_id,
        holder_pid,
    )
    from clio_agent.runtime.stream_audit import stream_audit  # noqa: PLC0415

    stream_audit(
        "launcher_cache_lock_stale_broken",
        reason=LAUNCHER_CACHE_LOCK_STALE_BROKEN,
        server_id=server_id,
        holder_pid=holder_pid,
    )
    with suppress(OSError):
        owner_path.unlink(missing_ok=True)
    with suppress(OSError):
        lock_path.unlink(missing_ok=True)


def uses_shared_launcher_cache(spec: object) -> bool:
    """True when ``spec`` (an :class:`MCPServerSpec`) spawns onto the SHARED
    dedicated uv cache — the only spawns this lock needs to serialize.

    Mirrors ``transport_for``'s own condition exactly (``"UV_CACHE_DIR" not in
    spec.env``): a declaration with its own explicit ``UV_CACHE_DIR`` opted out
    of the shared dir, so it cannot race another spawn ON it.
    """

    return (
        getattr(spec, "transport", "") == "stdio"
        and bool(getattr(spec, "command", ""))
        and "UV_CACHE_DIR" not in (getattr(spec, "env", None) or {})
    )


@contextmanager
def acquire_launcher_cache_lock(
    server_id: str, *, timeout_s: float | None = None
) -> Iterator[None]:
    """Liveness-driven wait for the shared uv-launcher cache lock (#1237).

    While the current holder is a LIVE process, this waits — however long
    the holder's legitimate cold-spawn work takes; never a deadline race
    (see the module docstring). A holder whose recorded PID is confirmed
    dead names an abandoned lock: it is broken (typed, loud via
    :func:`_break_stale_lock`) and acquisition retries immediately.
    ``timeout_s`` (default :func:`launcher_cache_lock_timeout_s`) is a
    GENEROUS runaway backstop, not a normal-path bound.
    """

    bound = timeout_s if timeout_s is not None else launcher_cache_lock_timeout_s()
    lock_path = _lock_path()
    owner_path = _owner_path(lock_path)
    lock = FileLock(str(lock_path), timeout=0)
    deadline = time.monotonic() + bound
    logged_wait = False
    while True:
        try:
            lock.acquire(timeout=0)
            break
        except Timeout:
            pass
        holder_pid = _read_owner_pid(owner_path)
        if holder_pid is not None and not _pid_alive(holder_pid):
            _break_stale_lock(lock_path, owner_path, server_id, holder_pid)
            continue
        if time.monotonic() >= deadline:
            logger.warning(
                "launcher_cache_lock_runaway reason=%s server=%s timeout_s=%.1f holder_pid=%s",
                LAUNCHER_CACHE_LOCK_TIMEOUT,
                server_id,
                bound,
                holder_pid,
            )
            from clio_agent.runtime.stream_audit import stream_audit  # noqa: PLC0415

            stream_audit(
                "launcher_cache_lock_timeout",
                reason=LAUNCHER_CACHE_LOCK_TIMEOUT,
                server_id=server_id,
                timeout_s=bound,
                holder_pid=holder_pid,
            )
            raise LauncherCacheLockTimeoutError(server_id, bound)
        if not logged_wait:
            logger.info(
                "launcher_cache_lock_waiting server=%s holder_pid=%s -- holder is alive, "
                "waiting (never racing a deadline; #1237)",
                server_id,
                holder_pid,
            )
            logged_wait = True
        time.sleep(_POLL_INTERVAL_S)
    try:
        _write_owner_pid(owner_path, os.getpid())
        yield
    finally:
        _clear_owner_pid(owner_path)
        with suppress(Exception):
            lock.release()


__all__ = [
    "LauncherCacheLockTimeoutError",
    "acquire_launcher_cache_lock",
    "launcher_cache_lock_timeout_s",
    "uses_shared_launcher_cache",
]
