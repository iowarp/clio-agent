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
* Progress is the lock changing hands: every hand-off (a sibling finished its
  spawn) restarts the clock, so a queue of cold spawns on a slow machine is
  waited out however long it is. Only ONE holder keeping the lock past
  :func:`launcher_cache_lock_hold_ceiling_s` -- longer than its own connect can
  take (``tools.mcp.max_wait_s``, the connect ceiling, plus one
  ``tools.mcp.no_progress_s`` window) -- is a wedged holder, typed and loud
  (:data:`clio_agent.errors.LAUNCHER_CACHE_LOCK_TIMEOUT`).
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager, suppress
from pathlib import Path

from filelock import FileLock, Timeout

from clio_agent.errors import LAUNCHER_CACHE_LOCK_STALE_BROKEN, LAUNCHER_CACHE_LOCK_TIMEOUT

logger = logging.getLogger(__name__)

_POLL_INTERVAL_S = 1.0
_LOCK_FILENAME = ".clio-launcher.lock"
_OWNER_SUFFIX = ".owner"


class LauncherCacheLockTimeoutError(RuntimeError):
    """One holder kept the launcher-cache lock past the hold ceiling with no hand-off.

    #1237: never the normal path. Every hand-off of the lock is progress and restarts
    the clock; this fires only for a holder (or an unidentifiable one) that kept it
    longer than its own connect can take.
    """

    def __init__(self, server_id: str, timeout_s: float, holder_pid: int | None = None) -> None:
        self.server_id = server_id
        self.timeout_s = timeout_s
        self.holder_pid = holder_pid
        super().__init__(
            f"MCP server {server_id!r}: the launcher cache lock was held by "
            f"{'pid ' + str(holder_pid) if holder_pid is not None else 'an unidentified holder'} "
            f"for {timeout_s:g}s with no hand-off (reason={LAUNCHER_CACHE_LOCK_TIMEOUT})"
        )


def launcher_cache_lock_hold_ceiling_s() -> float:
    """How long ONE holder may keep the lock before it is a wedged holder (seconds).

    The holder's own connect is bounded by ``tools.mcp.max_wait_s`` (progress-based,
    :mod:`clio_agent.tools.mcp_server_progress`); one ``tools.mcp.no_progress_s``
    window on top lets it finish and release. Not a deadline on the waiter: every
    hand-off restarts the clock.
    """
    from clio_agent.tools.mcp_server_progress import (  # noqa: PLC0415
        mcp_max_wait_s,
        mcp_no_progress_s,
    )

    return mcp_max_wait_s() + mcp_no_progress_s()


class _HolderClock:
    """Times how long the CURRENT holder has kept the lock; a hand-off restarts it."""

    def __init__(self, server_id: str, ceiling_s: float) -> None:
        self._server_id = server_id
        self._ceiling_s = ceiling_s
        self._holder: str | None = None
        self._since = time.monotonic()

    def check(self, holder: str | None, holder_pid: int | None) -> None:
        """Raise typed once one holder (``holder``: its record) kept the lock past the
        ceiling; a new holder record is progress (the lock changed hands)."""
        now = time.monotonic()
        if holder != self._holder:
            self._holder, self._since = holder, now
            return
        if now - self._since < self._ceiling_s:
            return
        logger.warning(
            "launcher_cache_lock_runaway reason=%s server=%s hold_ceiling_s=%.1f holder_pid=%s",
            LAUNCHER_CACHE_LOCK_TIMEOUT,
            self._server_id,
            self._ceiling_s,
            holder_pid,
        )
        from clio_agent.runtime.stream_audit import stream_audit  # noqa: PLC0415

        stream_audit(
            "launcher_cache_lock_timeout",
            reason=LAUNCHER_CACHE_LOCK_TIMEOUT,
            server_id=self._server_id,
            timeout_s=self._ceiling_s,
            holder_pid=holder_pid,
        )
        raise LauncherCacheLockTimeoutError(self._server_id, self._ceiling_s, holder_pid)


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


def _read_owner_record(owner_path: Path) -> str | None:
    """The raw holder record ``"<pid> <acquisition token>"`` (``None``: none/unreadable).

    The token tells two holders in ONE process apart (sibling namespaces' cold spawns
    in the same discovery pass), so a hand-off between them is seen as progress.
    """

    try:
        return owner_path.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def _read_owner_pid(owner_path: Path) -> int | None:
    """Best-effort read of the recorded holder PID (``None``: no/unreadable record)."""

    record = _read_owner_record(owner_path)
    try:
        return int(record.split()[0]) if record else None
    except ValueError:
        return None


def _write_owner_pid(owner_path: Path, pid: int) -> None:
    """Best-effort, near-atomic stamp of the current holder (write-then-rename)."""

    tmp_path = owner_path.with_name(f"{owner_path.name}.{pid}.tmp")
    try:
        tmp_path.write_text(f"{pid} {uuid.uuid4().hex}", encoding="utf-8")
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

    command = str(getattr(spec, "command", "") or "")
    return (
        getattr(spec, "transport", "") == "stdio"
        and bool(command)
        and "UV_CACHE_DIR" not in (getattr(spec, "env", None) or {})
        and _launcher_name(command) not in _SELF_ISOLATING_LAUNCHERS
    )


# Launchers that run each server from their own locked environment on their own uv
# cache (they set the child's ``UV_CACHE_DIR`` themselves), so their launches never
# touch the shared cache this lock guards and may start concurrently. clio-kit:
# ``_run_locked_local_server`` + ``environment_locks.EnvironmentInUseMarker``.
_SELF_ISOLATING_LAUNCHERS = frozenset({"clio-kit"})


def _launcher_name(command: str) -> str:
    """The launcher's bare name: ``C:\\...\\clio-kit.EXE`` and ``/.../clio-kit`` -> ``clio-kit``."""

    name = command.replace("\\", "/").rsplit("/", 1)[-1].lower()
    for suffix in (".exe", ".cmd", ".bat"):
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name


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
    ``timeout_s`` (default :func:`launcher_cache_lock_hold_ceiling_s`) bounds how
    long ONE holder may keep the lock; every hand-off restarts it.
    """

    bound = timeout_s if timeout_s is not None else launcher_cache_lock_hold_ceiling_s()
    lock_path = _lock_path()
    owner_path = _owner_path(lock_path)
    lock = FileLock(str(lock_path), timeout=0)
    clock = _HolderClock(server_id, bound)
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
        clock.check(_read_owner_record(owner_path), holder_pid)
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


@asynccontextmanager
async def aacquire_launcher_cache_lock(
    server_id: str, *, timeout_s: float | None = None
) -> AsyncIterator[None]:
    """Async twin of :func:`acquire_launcher_cache_lock` (#1237).

    For an async dispatch-time cold spawn (``mcp_executor.py``'s
    ``_connect_namespace``, the executor's on-demand connect for an ACTUAL
    tool call, as opposed to the discovery/listing pass) -- the identical
    liveness-driven wait, but polling via ``await asyncio.sleep`` and
    running the (fast, non-blocking-in-practice since ``timeout=0``, but
    still real file I/O) ``FileLock.acquire`` off the event loop via
    :func:`asyncio.to_thread` so a long wait here never stalls this
    executor's event loop (other executors/sessions are unaffected either
    way -- each has its own loop).

    ``thread_local=False`` is LOAD-BEARING: ``acquire`` runs on a worker
    thread (via ``to_thread``) but ``release`` runs directly on the event
    loop thread in the ``finally`` below -- filelock's default
    ``thread_local=True`` tracks acquisition per-thread, so a release from a
    DIFFERENT thread than the one that acquired silently fails to free the
    underlying OS lock (found live: the orphaned lock then wedges every
    future waiter, who can never identify a holder PID once the owner
    record is cleared, until the hold ceiling fires). Each
    call constructs a FRESH ``FileLock`` instance (never shared/reentrant
    across calls), so process-wide (non-thread-local) tracking is correct
    here regardless.
    """

    bound = timeout_s if timeout_s is not None else launcher_cache_lock_hold_ceiling_s()
    lock_path = _lock_path()
    owner_path = _owner_path(lock_path)
    lock = FileLock(str(lock_path), timeout=0, thread_local=False)
    clock = _HolderClock(server_id, bound)
    logged_wait = False
    while True:
        try:
            await asyncio.to_thread(lock.acquire, timeout=0)
            break
        except Timeout:
            pass
        holder_pid = _read_owner_pid(owner_path)
        if holder_pid is not None and not _pid_alive(holder_pid):
            _break_stale_lock(lock_path, owner_path, server_id, holder_pid)
            continue
        clock.check(_read_owner_record(owner_path), holder_pid)
        if not logged_wait:
            logger.info(
                "launcher_cache_lock_waiting server=%s holder_pid=%s -- holder is alive, "
                "waiting (never racing a deadline; #1237)",
                server_id,
                holder_pid,
            )
            logged_wait = True
        await asyncio.sleep(_POLL_INTERVAL_S)
    try:
        _write_owner_pid(owner_path, os.getpid())
        yield
    finally:
        _clear_owner_pid(owner_path)
        with suppress(Exception):
            lock.release()


__all__ = [
    "LauncherCacheLockTimeoutError",
    "aacquire_launcher_cache_lock",
    "acquire_launcher_cache_lock",
    "launcher_cache_lock_hold_ceiling_s",
    "uses_shared_launcher_cache",
]
