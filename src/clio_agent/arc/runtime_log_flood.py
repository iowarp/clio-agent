"""A clio-core daemon stuck in a log flood is surfaced, and replaced when safe (owner module).

clio-core 2.4.0 can enter an error path that never ends and logs on every
iteration (``GlobalDeserialize::read_binary: Attempted to read beyond end of
data``, about 200,000 lines/s, one core busy). The log pump
(:mod:`clio_agent.arc.runtime_log_pump`) bounds the file on disk and writes
``clio-runtime-flood.json`` while the daemon's line rate stays above
``arc.clio_core.log_flood_lines_per_s``. This module acts on that record:

* surfacing, always: one loud ERROR per flood from a background monitor in every
  attached process, and the ``clio_core_daemon_log_flood`` row of ``/v1/health`` and
  the doctor (:mod:`clio_agent.runtime.clio_core_log_health`);
* replacement at attach, when no live client uses the daemon: connect-or-spawn stops
  it (a bounded ``clio_run stop`` with the pidfile kill behind it) and spawns a
  fresh one on the same store, typed ``clio_core_daemon_replaced_after_log_flood``;
* replacement while clients are live, only when
  ``arc.clio_core.log_flood_restart_live`` / ``CLIO_ARC_CLIO_CORE_LOG_FLOOD_RESTART_LIVE``
  is set. It is opt-in because those clients lose their daemon
  (``ClioCoreRuntimeLostError``) until they attach again.

A record is acted on only while it is fresh and names the daemon the pidfile names,
so a stale record from an exited daemon or a dead pump never stops a healthy one.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: Typed reason: the daemon was flooding its log and was stopped and replaced.
DAEMON_REPLACED_AFTER_LOG_FLOOD = "clio_core_daemon_replaced_after_log_flood"
#: Typed reason on the surfacing ERROR / health row while a flood is active.
DAEMON_LOG_FLOOD = "clio_core_daemon_log_flood"
#: A record whose ``last_seen`` is older than this is stale (the pump refreshes every 2 s).
RECORD_FRESH_S = 30.0
MONITOR_POLL_S = 5.0

_replacement: dict[str, Any] = {}
_warned: set[tuple[Any, Any]] = set()
_monitor_lock = threading.Lock()
_monitor: threading.Thread | None = None


def restart_live_enabled() -> bool:
    """Opt-in switch to replace a flooding daemon that live clients still use (OFF)."""
    from clio_agent import conf  # noqa: PLC0415 - avoid import cycle at module load

    try:
        return conf.resolve(
            "arc.clio_core.log_flood_restart_live",
            env="CLIO_ARC_CLIO_CORE_LOG_FLOOD_RESTART_LIVE",
            default=False,
            cast=conf.as_bool,
        )
    except ValueError as exc:
        logger.warning(
            "ignoring unparseable CLIO_ARC_CLIO_CORE_LOG_FLOOD_RESTART_LIVE (reason=%s); "
            "a flooding daemon with live clients is surfaced, not stopped",
            exc,
        )
        return False


def active_flood(state_dir: Path | None = None, *, now: float | None = None) -> dict[str, Any]:
    """The fresh flood record of the daemon the pidfile names, else ``{}``."""
    from clio_agent.arc.clio_core_config import runtime_state_dir  # noqa: PLC0415
    from clio_agent.arc.pid_identity import pidfile_live_pid  # noqa: PLC0415
    from clio_agent.arc.runtime_log import flood_record_path  # noqa: PLC0415

    # A read-only probe: the monitor thread polls this for the process lifetime.
    state = runtime_state_dir(create=False) if state_dir is None else state_dir
    try:
        record = json.loads(flood_record_path(state).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(record, dict):
        return {}
    last_seen = record.get("last_seen")
    current = time.time() if now is None else now
    if not isinstance(last_seen, (int, float)) or current - last_seen > RECORD_FRESH_S:
        return {}
    daemon_pid = pidfile_live_pid(state / "clio-runtime.pid")
    if daemon_pid is None or record.get("daemon_pid") != daemon_pid:
        return {}
    return record


def _surface(record: dict[str, Any], *, live: list[int], action: str) -> None:
    """One ERROR per flood (daemon pid + first_seen), never one per poll."""
    key = (record.get("daemon_pid"), record.get("first_seen"))
    if key in _warned:
        return
    _warned.add(key)
    logger.error(
        "clio-core daemon pid=%s is flooding its log at %s lines/s (reason=%s); "
        "repeated line: %r; live clients=%s; %s. The log stays capped; set "
        "CLIO_ARC_CLIO_CORE_LOG_FLOOD_RESTART_LIVE=1 to replace a flooding daemon "
        "even while clients use it.",
        record.get("daemon_pid"),
        record.get("lines_per_s"),
        DAEMON_LOG_FLOOD,
        record.get("line"),
        sorted(live),
        action,
    )


def replace_flooding_daemon(config_path: str, log_level: str) -> bool:
    """Stop a flooding daemon nobody uses (any daemon on the opt-in); ``True`` when stopped.

    Called with the host-global spawn lock held. At attach this process has not
    registered yet, so ``live`` counts only other clients; the monitor calls it only
    on the opt-in, which stops the daemon even though this process uses it.
    """
    from clio_agent.arc import clio_core_daemon_version as daemon_version  # noqa: PLC0415
    from clio_agent.arc import crashed_clients, runtime_stop  # noqa: PLC0415
    from clio_agent.arc.clio_core_config import runtime_state_dir  # noqa: PLC0415

    record = active_flood()
    if not record:
        return False
    live, _crashed = crashed_clients.client_census(prune=False)
    if live and not restart_live_enabled():
        _surface(record, live=live, action="left running because live clients use it")
        return False
    _surface(record, live=live, action="stopping it and starting a fresh daemon")
    daemon_pid = record.get("daemon_pid")
    running = daemon_version.running_daemon_config(runtime_state_dir(), config_path)
    started = time.monotonic()
    outcome = runtime_stop.stop_runtime_daemon(
        running, log_level, ceiling_s=runtime_stop.stop_no_progress_s()
    )
    exited = crashed_clients._wait_for_exit(daemon_pid)
    found = {
        "reason": DAEMON_REPLACED_AFTER_LOG_FLOOD,
        "daemon_pid": daemon_pid,
        "line": record.get("line"),
        "lines_per_s": record.get("lines_per_s"),
        "live_clients": sorted(live),
        "stop_path": outcome.path,
        "exited": exited,
        "seconds": round(time.monotonic() - started, 2),
        "at": time.time(),
    }
    _replacement.clear()
    _replacement.update(found)
    if not exited:
        logger.error(
            "flooding clio-core daemon pid=%s did not exit after the stop (reason=%s)",
            daemon_pid,
            crashed_clients.DAEMON_REPLACEMENT_EXIT_UNCONFIRMED,
        )
    logger.warning("clio-core daemon replaced after log flood: %s", found)
    return True


def _poll_once() -> None:
    """One monitor pass: surface an active flood; replace it only on the opt-in."""
    from clio_agent.arc import crashed_clients, runtime_stop, storage  # noqa: PLC0415

    record = active_flood()
    if not record:
        return
    if not restart_live_enabled():
        live, _crashed = crashed_clients.client_census(prune=False)
        _surface(record, live=live, action="left running because live clients use it")
        return
    with storage._runtime_spawn_lock():
        if runtime_stop._runtime_shutdown_requested:
            return
        replace_flooding_daemon(storage._active_config_path, storage._active_log_level)


def _monitor_loop() -> None:
    while True:
        time.sleep(MONITOR_POLL_S)
        try:
            _poll_once()
        except (OSError, ValueError, RuntimeError) as exc:
            # A failed pass (a vanished state dir, a stop that raised) is logged and the
            # next pass retries; anything unexpected surfaces via threading.excepthook.
            logger.warning("clio-core log flood monitor pass failed: %r", exc)


def ensure_flood_monitor() -> None:
    """Start this process's flood monitor once (a daemon thread polling every 5 s)."""
    global _monitor
    with _monitor_lock:
        if _monitor is not None and _monitor.is_alive():
            return
        _monitor = threading.Thread(
            target=_monitor_loop, name="clio-runtime-log-flood-monitor", daemon=True
        )
        _monitor.start()


def flood_replacement() -> dict[str, Any]:
    """What :func:`replace_flooding_daemon` did in this process ({} if none)."""
    return dict(_replacement)


def reset_flood_state() -> None:
    """Forget the recorded replacement and surfaced floods (test seam)."""
    _replacement.clear()
    _warned.clear()
