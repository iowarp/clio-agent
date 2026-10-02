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
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Literal

from clio_agent.arc.clio_core_liveness import _resolve_runtime_port, _runtime_alive
from clio_agent.arc.runtime_crash import DaemonStillStarting, expect_daemon_exit
from clio_agent.arc.runtime_spawn import _dynamic_library_env_var, _runtime_launcher_path

logger = logging.getLogger(__name__)

# The stop waits as long as the daemon keeps working (CPU or I/O advancing); a slice
# this long with NO progress is a stall (see ``stop_runtime_daemon``). A long stretch --
# a daemon flushing to a slow disk pauses between writes -- yet half of the desktop
# supervisor's 30 s graceful-shutdown window, which this stop is one step inside.
# Configurable (``arc.liveness.stop_no_progress_s``), see :func:`stop_no_progress_s`.
_DEFAULT_STOP_NO_PROGRESS_S = 15.0
_RUNTIME_STOP_POLL_SECONDS = 0.1


def stop_no_progress_s() -> float:
    """``arc.liveness.stop_no_progress_s`` / ``CLIO_ARC_LIVENESS_STOP_NO_PROGRESS_S`` (15 s).

    How long a stopping daemon may make no progress (CPU or I/O flat) before it is
    killed. A non-positive value keeps the default.
    """
    from clio_agent import conf  # noqa: PLC0415 - avoid import cycle at module load

    value = conf.resolve(
        "arc.liveness.stop_no_progress_s",
        env="CLIO_ARC_LIVENESS_STOP_NO_PROGRESS_S",
        default=_DEFAULT_STOP_NO_PROGRESS_S,
        cast=conf.as_float,
    )
    return value if value > 0 else _DEFAULT_STOP_NO_PROGRESS_S


def _daemon_process_alive(pid: int | None, recorded_create_time: float | None) -> bool:
    """Whether the pidfile daemon's process still runs (``False`` when it is unknown).

    An exited child not yet reaped (a zombie) has stopped: it holds no port and does
    no work.
    """
    if pid is None:
        return False
    import psutil  # noqa: PLC0415

    from clio_agent.arc import storage  # noqa: PLC0415 - avoid storage import cycle

    if not storage._pid_alive(pid, recorded_create_time):
        return False
    try:
        return bool(psutil.Process(pid).status() != psutil.STATUS_ZOMBIE)
    except psutil.NoSuchProcess:
        return False  # exited between the two checks
    except psutil.AccessDenied:
        return True  # it exists (pid_alive said so); its state is just unreadable


StopPath = Literal[
    "clean_stop", "stall_kill", "helper_failed_kill", "launcher_missing_kill", "error_kill"
]


@dataclass(frozen=True)
class StopOutcome:
    """Result of one :func:`stop_runtime_daemon` attempt.

    Attributes:
        stopped: Whether the clean ``clio_run stop`` handshake observed the
            runtime port free itself (no hard pidfile kill was needed).
        path: Which of the stop paths this attempt took -- a structured
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


def kill_daemon_pidfile() -> None:
    """Terminate the PID-file daemon with PID-reuse protection."""

    from clio_agent.arc import storage  # noqa: PLC0415 - avoid storage import cycle

    pidfile = storage._daemon_pidfile()
    try:
        parts = pidfile.read_text(encoding="utf-8").split()
    except OSError:
        return
    if not parts:
        return
    try:
        pid = int(parts[0])
    except ValueError:
        return
    recorded = None
    if len(parts) > 1:
        with contextlib.suppress(ValueError):
            recorded = float(parts[1])
    if not storage._pid_alive(pid, recorded):
        with contextlib.suppress(OSError):
            pidfile.unlink()
        return
    import psutil  # noqa: PLC0415

    try:
        proc = psutil.Process(pid)
        # POSIX: a suspended (SIGSTOP) daemon only acts on the SIGTERM once continued;
        # left stopped it would cost the whole grace below before the SIGKILL. (Windows
        # terminates a suspended process outright.)
        suspended = os.name != "nt" and proc.status() == psutil.STATUS_STOPPED
        proc.terminate()
        if suspended:
            proc.resume()
        try:
            proc.wait(timeout=5.0)
        except psutil.TimeoutExpired:
            proc.kill()
    except psutil.NoSuchProcess:
        logger.info("clio-core daemon pid=%d already exited before the pidfile kill", pid)
    except psutil.Error as exc:
        # Inaccessible (AccessDenied) or a kill that did not take: the daemon may still be
        # running. Loud and typed; the pidfile is kept so the next stop can find it.
        logger.error(
            "clio-core daemon pidfile kill failed (reason=%s pid=%d error=%s: %s); the "
            "daemon may still be running",
            DAEMON_KILL_FAILED,
            pid,
            type(exc).__name__,
            exc,
        )
        return
    with contextlib.suppress(OSError):
        pidfile.unlink()


#: Typed reason for a pidfile kill that did not take (inaccessible or still alive).
DAEMON_KILL_FAILED = "clio_core_daemon_kill_failed"
#: Typed reasons for the two failed-startup cleanups (#1401, no silent fallback).
FAILED_SPAWN_KILLED = "clio_core_failed_spawn_killed"
FAILED_ATTACH_RELEASED = "clio_core_failed_attach_released"


@contextlib.contextmanager
def kill_spawned_daemon_on_failure() -> Iterator[None]:
    """Kill the daemon this process just spawned if it never became usable (#1401).

    Wraps spawn-and-wait under the host-global spawn lock. A daemon that fails to
    bind its port serves no client, and the port-based clean stop cannot see it (its
    port is already free), so a failed start used to leave a live ``clio_run`` with
    its pidfile later unlinked. This kills it by its pidfile (PID-reuse guarded)
    and drops this process's registration so it holds no last-one-out vote.
    """
    try:
        yield
    except DaemonStillStarting:
        from clio_agent.arc import storage  # noqa: PLC0415 - avoid storage import cycle

        # Still progressing at the ceiling: never killed. Its pidfile stays, so the next
        # attach adopts it once it binds; this process gives up only its own vote.
        logger.warning(
            "clio-core daemon still starting at the ceiling; left running for the next "
            "attach (reason=%s)",
            DaemonStillStarting.degradation_reason,
        )
        storage._deregister_client()
        raise
    except BaseException:
        from clio_agent.arc import storage  # noqa: PLC0415 - avoid storage import cycle

        logger.warning(
            "clio-core daemon start failed; killing the spawned daemon (reason=%s)",
            FAILED_SPAWN_KILLED,
        )
        storage._deregister_client()
        with contextlib.suppress(OSError, ValueError, IndexError):
            expect_daemon_exit(int(storage._daemon_pidfile().read_text("utf-8").split()[0]))
        kill_daemon_pidfile()
        raise


def release_failed_attach(config_path: str, log_level: str) -> None:
    """Give up a failed attach's vote; stop the daemon iff no live client remains (#1401).

    Runs when the native attach, the CTE init, or the post-attach probe fails after
    the daemon is up. Deregistering alone left a daemon this process may have just
    spawned running with no client at all; this applies the same last-one-out rule
    as a graceful release, under the same host-global lock.
    """
    from clio_agent.arc import storage  # noqa: PLC0415 - avoid storage import cycle

    with storage._runtime_spawn_lock():
        storage._deregister_client()
        if storage._live_client_pids():
            return
        logger.warning(
            "clio-core attach failed with no other live client; stopping the daemon (reason=%s)",
            FAILED_ATTACH_RELEASED,
        )
        storage._stop_runtime_daemon(config_path, log_level)


def cleanup_runtime_after_client_crash(
    config_path: str = "",
    log_level: str = "error",
    *,
    wait_timeout_seconds: float = 0.0,
) -> bool:
    """Prune crashed clients and stop clio-core only when none remain live."""

    from clio_agent.arc import storage  # noqa: PLC0415 - avoid storage import cycle

    deadline = time.monotonic() + max(wait_timeout_seconds, 0.0)
    while True:
        with storage._runtime_spawn_lock():
            if not storage._live_client_pids():
                storage._stop_runtime_daemon(
                    config_path or os.environ.get("CLIO_SERVER_CONF", ""), log_level
                )
                return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(min(0.1, max(deadline - time.monotonic(), 0.0)))


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
        parts = storage._daemon_pidfile().read_text(encoding="utf-8").split()
    except OSError:
        parts = []  # no pidfile: the port alone tells the stop
    daemon_pid = int(parts[0]) if parts and parts[0].isdigit() else None
    recorded_create_time: float | None = None
    if daemon_pid is not None and len(parts) > 1:
        with contextlib.suppress(ValueError):
            recorded_create_time = float(parts[1])
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
            # Wait on the DAEMON, not a fixed deadline: a durable daemon flushes its data
            # while stopping, which takes as long as the disk takes. The port freeing AND
            # the daemon process exiting is a clean stop; the daemon still working (CPU or
            # I/O advancing) keeps the wait going; a whole slice with no progress -- or
            # the long ceiling -- is a stall, killed loudly (data not yet flushed may be
            # lost), never silently.
            #
            # The port alone is not proof: a daemon that stopped running (suspended, or
            # hung without accepting) leaves its listener's accept backlog to fill with
            # the probes below, after which a probe times out exactly like a freed port.
            # Taking that for a clean stop left the daemon running with its pidfile gone.
            from clio_agent.arc.daemon_progress import max_wait_s  # noqa: PLC0415 - cycle
            from clio_agent.runtime.progress import process_work  # noqa: PLC0415

            ceiling = max_wait_s()
            no_progress_s = stop_no_progress_s()
            started = window = time.monotonic()
            last_work = process_work(daemon_pid) if daemon_pid is not None else None
            while True:
                if not _runtime_alive(runtime_port) and not _daemon_process_alive(
                    daemon_pid, recorded_create_time
                ):
                    stopped = True
                    path = "clean_stop"
                    break
                # The helper FAILING (non-zero exit: it could not load the config or
                # reach the daemon) means the stop request was never delivered: the
                # daemon is not stopping, so there is nothing to wait for. A helper
                # exiting 0 delivered it, and the daemon may still be flushing.
                helper_code = stop_process.poll()
                if helper_code not in (None, 0):
                    logger.warning(
                        "clio-core daemon stop request failed (reason=helper_failed "
                        "clio_run stop exited with code %d while the daemon still holds "
                        "port %d); killing it -- data not yet flushed may be lost",
                        helper_code,
                        runtime_port,
                    )
                    path = "helper_failed_kill"
                    break
                now = time.monotonic()
                if now - window >= no_progress_s:
                    work = process_work(daemon_pid) if daemon_pid is not None else None
                    if work is None or last_work is None or work - last_work < 0.01:
                        logger.warning(
                            "clio-core daemon stop made no progress for %.0fs (wait=%s); "
                            "killing it -- data not yet flushed may be lost",
                            now - window,
                            "daemon_pid_unresolved" if daemon_pid is None else "no_progress",
                        )
                        path = "stall_kill"
                        break
                    last_work, window = work, now
                if now - started >= ceiling:
                    logger.warning(
                        "clio-core daemon still stopping after %.0fs (arc.liveness.max_wait_s); "
                        "killing it -- data not yet flushed may be lost",
                        now - started,
                    )
                    path = "stall_kill"
                    break
                time.sleep(_RUNTIME_STOP_POLL_SECONDS)
            if stop_process.poll() is None:
                stop_process.terminate()
                try:
                    stop_process.wait(timeout=1.0)
                except subprocess.TimeoutExpired:
                    stop_process.kill()
                    stop_process.wait(timeout=1.0)
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
