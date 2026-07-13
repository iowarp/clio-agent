"""Shared clio-core runtime *daemon* memory surfacing + bounded recycle (owner module, #891).

The shared clio-core runtime (``clio_run``, RPC port 9413) is a host-global singleton
that outlives any single client. It can grow *daemon-internally* — heap/arena/thread
growth the ARC data-tier ram cap (#890) does not bound — and today NOTHING in clio
surfaces that growth: it was found only when the user opened Task Manager (12.3 GiB
resident / 20.2 GiB committed after hours of session churn). Per the no-silent-
degradation ground rule, this module makes daemon memory **visible** (a doctor row via
:func:`clio_agent.runtime.clio_core_health.probe_clio_core_daemon_memory`) and
**bounded-by-policy** (an opt-in recycle seam).

Two capabilities, both owned here so ``arc.storage`` stays a thin call site under its
size ratchet:

* **Snapshot** (:func:`collect_daemon_memory_snapshot`): find the daemon PID via the
  pidfile seam (``storage._daemon_pidfile``), falling back to the process listening on
  the resolved runtime port, and read WorkingSet (RSS) + committed (VMS) + thread count
  + registered-client counts (live vs stale, via ``storage._live_client_pids``).
  :func:`classify_daemon_rss` maps RSS to a typed ``ok`` | ``elevated`` (>1 GiB) |
  ``critical`` (>4 GiB) status; the thresholds are config-resolvable.

* **Recycle** (:func:`maybe_recycle_idle_daemon`): opt-in (default OFF). When the probe
  is ``critical`` AND there are **zero LIVE registered clients**, it stops the daemon
  via the existing clean-stop-with-pidfile-fallback teardown
  (``storage._stop_runtime_daemon``) and prunes stale registrations; the next
  ``make_arc_store`` spawns a fresh, capped daemon. It **NEVER** recycles while a live
  client is attached, and every recycle emits a typed reason
  (:data:`CLIO_CORE_DAEMON_RECYCLED` with the before-RSS), mirroring the #897 init-
  degradation record — a loud, queryable action, not a silent restart.

This is process-external state (a shared daemon), but the recorded recycle reason is
process-local (like #897): the process that performed the recycle surfaces it.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Any, Callable, Optional, Protocol, runtime_checkable

logger = logging.getLogger(__name__)


@runtime_checkable
class _DaemonProcess(Protocol):
    """The psutil.Process surface this module reads (also satisfied by test fakes)."""

    pid: int

    def memory_info(self) -> Any: ...

    def num_threads(self) -> int: ...

# Typed reason code for a policy-driven daemon recycle. Named in the #897 vocabulary
# style so operators read one consistent language across clio-core degradations.
CLIO_CORE_DAEMON_RECYCLED = "clio_core_daemon_recycled"

# Default RSS thresholds (bytes). A daemon above the warn line is ``elevated``; above
# the critical line it is ``critical`` (recycle-eligible when idle). Config-resolvable
# via ``arc.clio_core.daemon_rss_warn_bytes`` / env ``CLIO_ARC_CLIO_CORE_DAEMON_RSS_WARN``
# and the ``_critical_bytes`` / ``_CRITICAL`` pair.
_DEFAULT_DAEMON_RSS_WARN_BYTES = 1 * 1024**3  # 1 GiB
_DEFAULT_DAEMON_RSS_CRITICAL_BYTES = 4 * 1024**3  # 4 GiB


@dataclass(frozen=True)
class DaemonMemorySnapshot:
    """A point-in-time reading of the shared clio-core daemon's memory + clients."""

    pid: int
    pid_source: str  # "pidfile" | "port_scan" | "injected"
    rss_bytes: int  # WorkingSet (resident)
    committed_bytes: int  # VMS / pagefile (committed)
    thread_count: int
    live_client_count: int
    stale_client_count: int
    registered_client_count: int
    port: int

    def to_details(self) -> dict[str, object]:
        """JSON-safe detail payload for the doctor row."""
        return {
            "pid": self.pid,
            "pid_source": self.pid_source,
            "rss_bytes": self.rss_bytes,
            "committed_bytes": self.committed_bytes,
            "thread_count": self.thread_count,
            "live_client_count": self.live_client_count,
            "stale_client_count": self.stale_client_count,
            "registered_client_count": self.registered_client_count,
            "port": self.port,
        }


@dataclass(frozen=True)
class DaemonRecycleOutcome:
    """The typed result of a :func:`maybe_recycle_idle_daemon` call (never silent)."""

    recycled: bool
    reason: str  # "disabled" | "no_daemon" | "not_critical" | "live_clients_present" | "recycled"
    before_rss_bytes: Optional[int] = None
    live_client_count: Optional[int] = None
    pid: Optional[int] = None


@dataclass(frozen=True)
class ClioCoreDaemonRecycle:
    """A recorded policy-driven daemon recycle (#891), mirroring the #897 record."""

    reason: str  # CLIO_CORE_DAEMON_RECYCLED
    before_rss_bytes: int
    before_committed_bytes: int
    thread_count: int
    pid: int
    port: int

    def to_details(self) -> dict[str, object]:
        """JSON-safe detail payload."""
        return {
            "reason": self.reason,
            "before_rss_bytes": self.before_rss_bytes,
            "before_committed_bytes": self.before_committed_bytes,
            "thread_count": self.thread_count,
            "pid": self.pid,
            "port": self.port,
        }


# --------------------------------------------------------------------------- #
# Config resolution
# --------------------------------------------------------------------------- #


def _resolve_daemon_rss_thresholds() -> tuple[int, int]:
    """Resolve (warn, critical) RSS byte thresholds; fail-safe to the defaults.

    Reads ``arc.clio_core.daemon_rss_warn_bytes`` / ``CLIO_ARC_CLIO_CORE_DAEMON_RSS_WARN``
    and the ``_critical_bytes`` / ``_CRITICAL`` pair. Non-positive, unparseable, or
    inverted (warn >= critical) values are refused with a WARNING (no silent accept of a
    threshold that would disable the surfacing) and the defaults are used.
    """
    from clio_agent import conf  # noqa: PLC0415 - avoid import cycle at module load

    try:
        warn = conf.resolve(
            "arc.clio_core.daemon_rss_warn_bytes",
            env="CLIO_ARC_CLIO_CORE_DAEMON_RSS_WARN",
            default=_DEFAULT_DAEMON_RSS_WARN_BYTES,
            cast=conf.as_int,
        )
        critical = conf.resolve(
            "arc.clio_core.daemon_rss_critical_bytes",
            env="CLIO_ARC_CLIO_CORE_DAEMON_RSS_CRITICAL",
            default=_DEFAULT_DAEMON_RSS_CRITICAL_BYTES,
            cast=conf.as_int,
        )
    except (ValueError, TypeError) as exc:
        logger.warning(
            "ignoring unparseable clio-core daemon RSS threshold(s) (reason=%s: %s); "
            "using defaults warn=%d critical=%d",
            type(exc).__name__,
            exc,
            _DEFAULT_DAEMON_RSS_WARN_BYTES,
            _DEFAULT_DAEMON_RSS_CRITICAL_BYTES,
        )
        return _DEFAULT_DAEMON_RSS_WARN_BYTES, _DEFAULT_DAEMON_RSS_CRITICAL_BYTES
    if warn <= 0 or critical <= 0 or warn >= critical:
        logger.warning(
            "refusing invalid clio-core daemon RSS thresholds (warn=%d critical=%d); "
            "using defaults warn=%d critical=%d",
            warn,
            critical,
            _DEFAULT_DAEMON_RSS_WARN_BYTES,
            _DEFAULT_DAEMON_RSS_CRITICAL_BYTES,
        )
        return _DEFAULT_DAEMON_RSS_WARN_BYTES, _DEFAULT_DAEMON_RSS_CRITICAL_BYTES
    return warn, critical


def _resolve_recycle_enabled() -> bool:
    """Resolve the opt-in recycle switch (default OFF); fail-safe to OFF.

    Reads ``arc.clio_core.daemon_recycle_enabled`` / ``CLIO_ARC_CLIO_CORE_DAEMON_RECYCLE``.
    A malformed value must NOT silently enable a daemon-killing action, so it falls back
    to OFF with a WARNING.
    """
    from clio_agent import conf  # noqa: PLC0415 - avoid import cycle at module load

    try:
        return conf.resolve(
            "arc.clio_core.daemon_recycle_enabled",
            env="CLIO_ARC_CLIO_CORE_DAEMON_RECYCLE",
            default=False,
            cast=conf.as_bool,
        )
    except ValueError as exc:
        logger.warning(
            "ignoring unparseable CLIO_ARC_CLIO_CORE_DAEMON_RECYCLE (reason=%s); "
            "recycle stays OFF",
            exc,
        )
        return False


def classify_daemon_rss(
    rss_bytes: int,
    *,
    warn: Optional[int] = None,
    critical: Optional[int] = None,
) -> str:
    """Classify a daemon RSS into ``ok`` | ``elevated`` | ``critical``.

    Args:
        rss_bytes: The resident set size to classify.
        warn: The elevated threshold; resolved from config when ``None``.
        critical: The critical threshold; resolved from config when ``None``.

    Returns:
        ``"critical"`` (>= critical), ``"elevated"`` (>= warn), else ``"ok"``.
    """
    if warn is None or critical is None:
        resolved_warn, resolved_critical = _resolve_daemon_rss_thresholds()
        warn = resolved_warn if warn is None else warn
        critical = resolved_critical if critical is None else critical
    if rss_bytes >= critical:
        return "critical"
    if rss_bytes >= warn:
        return "elevated"
    return "ok"


# --------------------------------------------------------------------------- #
# PID resolution + snapshot
# --------------------------------------------------------------------------- #


def _resolve_daemon_pid(config_path: str, env: Mapping[str, str] | None) -> tuple[Optional[int], str]:
    """Find the shared daemon PID: pidfile first, then the port-listener fallback.

    Returns ``(pid, source)`` where source is ``"pidfile"`` or ``"port_scan"``, or
    ``(None, "")`` when no live daemon can be located.
    """
    from clio_agent.arc import storage  # noqa: PLC0415 - lazy: storage is heavy, avoid cycle
    from clio_agent.arc.clio_core_liveness import _resolve_runtime_port  # noqa: PLC0415

    pidfile = storage._daemon_pidfile()
    try:
        parts = pidfile.read_text(encoding="utf-8").split()
        pid = int(parts[0])
        recorded: Optional[float] = None
        if len(parts) > 1 and parts[1]:
            recorded = float(parts[1])
        if storage._pid_alive(pid, recorded):
            return pid, "pidfile"
    except (OSError, ValueError, IndexError):
        pass

    port = _resolve_runtime_port(config_path)
    listener_pid = _pid_listening_on_port(port)
    if listener_pid is not None:
        return listener_pid, "port_scan"
    return None, ""


def _pid_listening_on_port(port: int) -> Optional[int]:
    """Return the PID of the process LISTENing on ``127.0.0.1:port``, or None.

    Best-effort psutil scan (may be denied for foreign-owned sockets); the pidfile is
    the primary source and this is only the fallback.
    """
    try:
        import psutil  # noqa: PLC0415

        for conn in psutil.net_connections(kind="inet"):
            if (
                conn.laddr
                and getattr(conn.laddr, "port", None) == port
                and conn.status == psutil.CONN_LISTEN
                and conn.pid is not None
            ):
                return int(conn.pid)
    except Exception:  # noqa: BLE001 - AccessDenied / import miss => "unknown"
        return None
    return None


def _registered_client_pids() -> list[int]:
    """List every registered client PID on disk WITHOUT pruning (total, incl. stale)."""
    from clio_agent.arc import storage  # noqa: PLC0415 - lazy: avoid heavy import at load

    reg = storage._client_registry_dir()
    if not reg.is_dir():
        return []
    return [int(entry.name) for entry in reg.iterdir() if entry.name.isdigit()]


def collect_daemon_memory_snapshot(
    *,
    env: Mapping[str, str] | None = None,
    config_path: str = "",
    process: _DaemonProcess | None = None,
    pid: Optional[int] = None,
    pid_source: Optional[str] = None,
    live_pids: Optional[list[int]] = None,
    registered_pids: Optional[list[int]] = None,
) -> Optional[DaemonMemorySnapshot]:
    """Read the shared daemon's memory + client counts, or None when no daemon is found.

    Args:
        env: Environment mapping (for port resolution); defaults to the process env.
        config_path: Optional clio-core config path for port resolution.
        process: An injected psutil-``Process``-like object (test seam); when given, its
            ``memory_info()`` / ``num_threads()`` are read directly.
        pid: An injected PID (test seam); resolved from the pidfile/port when ``None``.
        pid_source: Label for an injected PID; defaults to ``"injected"``.
        live_pids: Injected live-client PID list (test seam); read from the registry
            (``storage._live_client_pids``, which prunes stale) when ``None``.
        registered_pids: Injected total registered PID list (test seam); read from the
            registry dir when ``None``.

    Returns:
        A :class:`DaemonMemorySnapshot`, or ``None`` when the daemon is absent / the
        process vanished / psutil cannot read it.
    """
    from clio_agent.arc.clio_core_liveness import _resolve_runtime_port  # noqa: PLC0415

    resolved_source = pid_source
    if process is None:
        if pid is None:
            pid, resolved_source = _resolve_daemon_pid(config_path, env)
            if pid is None:
                return None
        try:
            import psutil  # noqa: PLC0415

            process = psutil.Process(pid)
        except Exception:  # noqa: BLE001 - NoSuchProcess / import miss => nothing to measure
            return None
    if pid is None:
        pid = int(getattr(process, "pid", -1))
    if resolved_source is None:
        resolved_source = "injected"

    try:
        mem = process.memory_info()
        rss_bytes = int(mem.rss)
        committed_bytes = int(mem.vms)
        thread_count = int(process.num_threads())
    except Exception:  # noqa: BLE001 - process vanished mid-read => nothing to measure
        return None

    live = live_pids if live_pids is not None else _live_client_pids_safe()
    registered = registered_pids if registered_pids is not None else _registered_client_pids()
    stale = max(0, len(registered) - len(live))
    port = _resolve_runtime_port(config_path)
    return DaemonMemorySnapshot(
        pid=pid,
        pid_source=resolved_source,
        rss_bytes=rss_bytes,
        committed_bytes=committed_bytes,
        thread_count=thread_count,
        live_client_count=len(live),
        stale_client_count=stale,
        registered_client_count=len(registered),
        port=port,
    )


def _live_client_pids_safe() -> list[int]:
    """``storage._live_client_pids`` with a lazy import (avoids a load-time cycle)."""
    from clio_agent.arc import storage  # noqa: PLC0415

    return storage._live_client_pids()


# --------------------------------------------------------------------------- #
# Recycle (opt-in, default OFF) + typed reason record
# --------------------------------------------------------------------------- #

_reason_lock = threading.Lock()
_last_recycle: ClioCoreDaemonRecycle | None = None


def record_clio_core_daemon_recycle(snapshot: DaemonMemorySnapshot) -> ClioCoreDaemonRecycle:
    """Record a policy-driven daemon recycle + emit the loud log line (#891).

    Mirrors :func:`clio_agent.arc.init_degradation.record_arc_init_degradation`: a typed
    process-local record other in-process code (and the trace) can read, never a silent
    restart.
    """
    record = ClioCoreDaemonRecycle(
        reason=CLIO_CORE_DAEMON_RECYCLED,
        before_rss_bytes=snapshot.rss_bytes,
        before_committed_bytes=snapshot.committed_bytes,
        thread_count=snapshot.thread_count,
        pid=snapshot.pid,
        port=snapshot.port,
    )
    with _reason_lock:
        global _last_recycle
        _last_recycle = record
    logger.warning(
        "recycled the shared clio-core daemon under policy (reason=%s pid=%d "
        "before_rss=%d before_committed=%d threads=%d): it was CRITICAL with zero live "
        "clients; the next ARC client spawns a fresh, capped daemon. This is a LOUD, "
        "policy-driven recycle (#891), not a silent restart.",
        record.reason,
        record.pid,
        record.before_rss_bytes,
        record.before_committed_bytes,
        record.thread_count,
    )
    return record


def clio_core_daemon_recycle_snapshot() -> ClioCoreDaemonRecycle | None:
    """Return the most-recent recorded recycle in this process, or None."""
    with _reason_lock:
        return _last_recycle


def reset_clio_core_daemon_recycle() -> None:
    """Clear the recorded recycle (test seam; not used in production)."""
    with _reason_lock:
        global _last_recycle
        _last_recycle = None


def maybe_recycle_idle_daemon(
    *,
    env: Mapping[str, str] | None = None,
    snapshot: Optional[DaemonMemorySnapshot] = None,
    enabled: Optional[bool] = None,
    config_path: str = "",
    log_level: str = "error",
    live_pids_fn: Optional[Callable[[], list[int]]] = None,
    stop_daemon: Optional[Callable[[], None]] = None,
    lock: Optional[AbstractContextManager[None]] = None,
) -> DaemonRecycleOutcome:
    """Recycle the shared daemon iff CRITICAL **and** zero LIVE clients (opt-in, OFF).

    The live-client guard is the invariant: a daemon with **any** attached live client
    is NEVER recycled (that would rip the store out from under a running turn). The
    live-client check is taken fresh under the runtime spawn lock (serialised against a
    concurrent client attach/release, exactly like ``storage.release_runtime_client``),
    not read from ``snapshot`` — so a client that attached after the snapshot still
    blocks the recycle.

    Args:
        env: Environment mapping forwarded to the snapshot/threshold resolution.
        snapshot: A pre-taken memory snapshot; gathered fresh when ``None``.
        enabled: Force the opt-in switch (test seam); resolved from config when ``None``.
        config_path: clio-core config path forwarded to the teardown.
        log_level: log level forwarded to the teardown.
        live_pids_fn: Live-client PID provider (test seam); defaults to
            ``storage._live_client_pids``.
        stop_daemon: Teardown callable (test seam); defaults to
            ``storage._stop_runtime_daemon``.
        lock: Serialising context manager (test seam); defaults to the runtime spawn lock.

    Returns:
        A typed :class:`DaemonRecycleOutcome` describing exactly what happened (never a
        silent no-op).
    """
    if enabled is None:
        enabled = _resolve_recycle_enabled()
    if not enabled:
        logger.debug("clio-core daemon recycle is disabled (opt-in, default OFF); skipping")
        return DaemonRecycleOutcome(recycled=False, reason="disabled")

    if snapshot is None:
        snapshot = collect_daemon_memory_snapshot(env=env, config_path=config_path)
    if snapshot is None:
        return DaemonRecycleOutcome(recycled=False, reason="no_daemon")

    status = classify_daemon_rss(snapshot.rss_bytes)
    if status != "critical":
        return DaemonRecycleOutcome(
            recycled=False,
            reason="not_critical",
            before_rss_bytes=snapshot.rss_bytes,
            pid=snapshot.pid,
        )

    if live_pids_fn is None:
        live_pids_fn = _live_client_pids_safe
    if lock is None:
        from clio_agent.arc import storage  # noqa: PLC0415

        lock = storage._runtime_spawn_lock()

    with lock:
        live = live_pids_fn()
        if live:
            # INVARIANT: never recycle out from under a live client.
            logger.info(
                "clio-core daemon is CRITICAL (rss=%d) but %d live client(s) are "
                "attached; refusing to recycle",
                snapshot.rss_bytes,
                len(live),
            )
            return DaemonRecycleOutcome(
                recycled=False,
                reason="live_clients_present",
                before_rss_bytes=snapshot.rss_bytes,
                live_client_count=len(live),
                pid=snapshot.pid,
            )

        if stop_daemon is None:
            stop_daemon = _default_stop_daemon(config_path, log_level)
        stop_daemon()
        # Prune the now-certainly-stale registry entries (all clients were dead).
        live_pids_fn()
        record_clio_core_daemon_recycle(snapshot)
        return DaemonRecycleOutcome(
            recycled=True,
            reason="recycled",
            before_rss_bytes=snapshot.rss_bytes,
            live_client_count=0,
            pid=snapshot.pid,
        )


def _default_stop_daemon(config_path: str, log_level: str) -> Callable[[], None]:
    """Bind the storage clean-stop teardown (pidfile-fallback) as a zero-arg callable."""

    def _stop() -> None:
        from clio_agent.arc import storage  # noqa: PLC0415

        storage._stop_runtime_daemon(config_path or storage._active_config_path, log_level)

    return _stop
