"""Clean-stop path for the shared clio-core runtime daemon (issue #765 (c)).

Split out of ``arc/storage.py`` (file-size ratchet, #775/#774) -- this module
owns the "last client releases the daemon" clean-stop sequence: build a
``clio_run stop`` command with the SAME cross-platform helpers as the spawn
path (``_runtime_launcher_path`` for the ``.exe``-aware launcher name and
``_dynamic_library_env_var`` for the OS shared-library path variable), poll
until the runtime actually frees its port, and fall back to a hard pidfile
kill if the clean stop stalls or the launcher cannot be found.

The pidfile registry itself (``_daemon_pidfile`` / ``_kill_daemon_pidfile``)
stays owned by :mod:`clio_agent.arc.storage` -- a handful of other modules
(``clio_core_daemon.py``, ``runtime/disk_gc.py``, ``runtime/process_census.py``,
``runtime/status.py``, tests) already reach it as ``storage._daemon_pidfile()``.
This module therefore imports ``storage`` LAZILY, inside
:func:`stop_runtime_daemon`, rather than at module scope: ``storage.py``
imports this module (to implement ``release_runtime_client``), so a
module-level import here back into ``storage`` would be circular.

This module also owns the shutdown latch (:func:`prepare_runtime_shutdown` /
:func:`reset_runtime_shutdown` / :class:`RuntimeShutdownInProgress`) that
forbids reacquiring the shared runtime once a desktop-managed process has
started tearing down. It moved here from ``storage.py`` (rather than growing
that module past its file-size ratchet baseline, #775/#774) alongside the
clean-stop sequence it gates; ``storage.py`` re-exports all three names for
its existing callers.
"""

from __future__ import annotations

import contextlib
import logging
import os
import subprocess
import time
from dataclasses import dataclass
from typing import Literal

from clio_agent.arc.clio_core_liveness import _resolve_runtime_port, _runtime_alive
from clio_agent.arc.runtime_crash import expect_daemon_exit
from clio_agent.arc.runtime_spawn import _dynamic_library_env_var, _runtime_launcher_path

logger = logging.getLogger(__name__)

# desktop supervisor budget (GRACEFUL_SHUTDOWN_STALL) is 30s total from the 202
# response to a force-kill; this stop attempt is only ONE step inside that window
# (the turn drain + agent-task executor joins run around it), so it must leave
# headroom rather than spend the whole 30s itself.
_RUNTIME_STOP_STALL_SECONDS = 10.0
_RUNTIME_STOP_POLL_SECONDS = 0.1

# "clio_run stop" reporting success (the helper process exiting) and the daemon's
# listening socket actually closing are not perfectly atomic -- a genuine clean
# stop can observe the helper exited a few polls before the port reads free. This
# grace window (same poll cadence as the main loop) keeps that ordinary case from
# being misclassified as helper_exit_kill (and hard-killed) on every run.
_HELPER_EXIT_GRACE_SECONDS = 1.0

StopPath = Literal[
    "clean_stop", "stall_kill", "helper_exit_kill", "launcher_missing_kill", "error_kill"
]


@dataclass(frozen=True)
class StopOutcome:
    """Result of one :func:`stop_runtime_daemon` attempt.

    Attributes:
        stopped: Whether the clean ``clio_run stop`` handshake observed the
            runtime port free itself (no hard pidfile kill was needed).
        path: Which of the five stop paths this attempt took -- a structured
            reason for the trace/log, never a control-flow signal (every
            degraded path here already carries its own typed reason, #775
            no-silent-fallback).
    """

    stopped: bool
    path: StopPath


_runtime_shutdown_requested = False  # desktop Quit forbids late runtime reacquisition


class RuntimeShutdownInProgress(RuntimeError):
    """Raised when a caller tries to (re)acquire the shared runtime mid-shutdown."""


def prepare_runtime_shutdown() -> None:
    """Prevent work still unwinding during process shutdown from reacquiring ARC.

    Desktop Quit releases the shared daemon before the rest of the application
    teardown because provider and tool workers may take longer to join.  Marking
    the process first closes the race where such a worker could register this
    dying process again after its runtime client has been released.
    """

    global _runtime_shutdown_requested
    _runtime_shutdown_requested = True


def reset_runtime_shutdown() -> None:
    """Clear the shutdown latch so a later app lifecycle can reacquire the runtime.

    The latch is process-global, not lifespan-scoped: without a reset, a second
    ``build_app``/lifespan boot inside the SAME interpreter (a desktop relaunch
    that reuses the process, or a test harness building a second app) could never
    reacquire the shared clio-core runtime again after the first Quit latched it.
    Called at lifespan boot (``desktop_lifecycle.reset_for_boot``).
    """

    global _runtime_shutdown_requested
    _runtime_shutdown_requested = False


def stop_runtime_daemon(config_path: str, log_level: str) -> StopOutcome:
    """Stop the shared daemon cleanly (``clio_run stop``), with a kill fallback.

    Mirrors the spawn path: the launcher name (``.exe`` on Windows) comes from
    ``_runtime_launcher_path`` and the shared-library env var (``PATH`` /
    ``DYLD_LIBRARY_PATH`` / ``LD_LIBRARY_PATH``) from ``_dynamic_library_env_var``,
    so the clean stop works on every platform the spawn does (issue #765).
    """
    from clio_agent.arc import storage  # noqa: PLC0415 - lazy: storage imports this module

    stopped = False
    path: StopPath = "error_kill"
    try:
        daemon_pid = int(storage._daemon_pidfile().read_text(encoding="utf-8").split()[0])
    except (OSError, ValueError, IndexError):
        daemon_pid = None
    if daemon_pid is not None:
        expect_daemon_exit(daemon_pid)
    try:
        import iowarp_core  # noqa: PLC0415

        exe = _runtime_launcher_path(iowarp_core)
        if exe is None:
            logger.warning(
                "clean clio-core daemon stop unavailable "
                "(reason=launcher_not_found bin_dir=%r); falling back to pidfile kill",
                iowarp_core.get_bin_dir(),  # type: ignore[attr-defined]
            )
            path = "launcher_missing_kill"
        else:
            env = os.environ.copy()
            lib_var = _dynamic_library_env_var()
            env[lib_var] = (
                iowarp_core.get_lib_dir() + os.pathsep + env.get(lib_var, "")  # type: ignore[attr-defined]
            )
            env.setdefault("CTP_LOG_LEVEL", log_level)
            if config_path:
                env["CLIO_SERVER_CONF"] = config_path
            runtime_port = _resolve_runtime_port(config_path)
            stop_process = subprocess.Popen(  # noqa: S603 - fixed launcher path
                [exe, "stop"],
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            stall_deadline = time.monotonic() + _RUNTIME_STOP_STALL_SECONDS
            while True:
                helper_status = stop_process.poll()
                runtime_is_alive = _runtime_alive(runtime_port)
                if not runtime_is_alive:
                    stopped = True
                    path = "clean_stop"
                    if helper_status is None:
                        stop_process.terminate()
                        try:
                            stop_process.wait(timeout=1.0)
                        except subprocess.TimeoutExpired:
                            stop_process.kill()
                            stop_process.wait(timeout=1.0)
                    break
                if helper_status is not None:
                    # The helper already exited; give the port a brief grace
                    # window to actually free before conceding a hard kill.
                    grace_deadline = time.monotonic() + _HELPER_EXIT_GRACE_SECONDS
                    freed_during_grace = False
                    while time.monotonic() < grace_deadline:
                        if not _runtime_alive(runtime_port):
                            freed_during_grace = True
                            break
                        time.sleep(_RUNTIME_STOP_POLL_SECONDS)
                    if freed_during_grace:
                        stopped = True
                        path = "clean_stop"
                    else:
                        path = "helper_exit_kill"
                    break
                if time.monotonic() >= stall_deadline:
                    logger.warning(
                        "clean clio-core daemon stop stalled while runtime remained live; "
                        "falling back to pidfile kill"
                    )
                    path = "stall_kill"
                    stop_process.terminate()
                    try:
                        stop_process.wait(timeout=1.0)
                    except subprocess.TimeoutExpired:
                        stop_process.kill()
                        stop_process.wait(timeout=1.0)
                    break
                time.sleep(_RUNTIME_STOP_POLL_SECONDS)
    except (subprocess.TimeoutExpired, OSError, ImportError) as exc:
        logger.warning(
            "clean clio-core daemon stop failed (reason=%s: %s); falling back to pidfile kill",
            type(exc).__name__,
            exc,
        )
        stopped = False
        path = "error_kill"
    # Confirm the port actually freed; fall back to a direct kill if not.
    if not stopped or _runtime_alive(_resolve_runtime_port(config_path)):
        storage._kill_daemon_pidfile()
    with contextlib.suppress(OSError):
        storage._daemon_pidfile().unlink()
    outcome = StopOutcome(stopped=stopped, path=path)
    if outcome.stopped:
        logger.info(
            "released last clio-core client -> stopped shared runtime daemon (path=%s)",
            outcome.path,
        )
    else:
        logger.warning(
            "released last clio-core client -> shared runtime daemon required a hard kill "
            "(path=%s)",
            outcome.path,
        )
    return outcome
