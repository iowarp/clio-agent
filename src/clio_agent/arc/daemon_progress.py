"""Wait while the awaited work is visibly progressing; never a fixed deadline.

A slow machine (or a busy daemon) answers late; a zombie never answers. From the
caller's side both look the same, so a fixed wall-clock bound turns a slow-but-healthy
peer into a failure -- on a first run, on a laptop, under a test suite. The signal that
tells them apart is the awaited process's own work: one working through a backlog keeps
consuming CPU or doing I/O, a hung one does neither.

:func:`wait_while_progressing` waits in slices; after each slice without an answer it
samples a work counter: advanced -> still working, keep waiting (logged); otherwise the
wait ends with a typed :class:`WaitOutcome`. A long configurable ceiling
(``arc.liveness.max_wait_s``, 180 s) bounds even a busy peer that never answers.

The work counter measures the AWAITED process only:

* :func:`daemon_work` -- the clio-core daemon this process attached to (pidfile, then
  the listener on the configured port). A daemon that cannot be located is reported as
  :data:`DAEMON_PID_UNRESOLVED`, never as a stall.
* :class:`ProcessTreeWork` -- one process and its descendants (an MCP server and the
  launcher work under it), cumulative, scoped to one wait (bounded memory).
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

_MIN_WORK_PROGRESS = 0.01  # the work counter must advance by at least this per slice
_DEFAULT_MAX_WAIT_S = 180.0
_EXIT_WORK = 0.1  # work credited for a tree member that exited between two samples

#: Typed wait outcomes (queryable in logs and error details).
DONE = "done"
NO_PROGRESS = "no_progress"
CEILING = "ceiling"
DAEMON_PID_UNRESOLVED = "daemon_pid_unresolved"


class DaemonPidUnresolved(LookupError):
    """The clio-core daemon's process could not be located (no live pidfile, no listener)."""


@dataclass(frozen=True)
class WaitOutcome:
    """How one :func:`wait_while_progressing` ended.

    Attributes:
        reason: :data:`DONE`, :data:`NO_PROGRESS` (a whole slice without an answer and
            without work), :data:`CEILING` (still working at the ceiling) or
            :data:`DAEMON_PID_UNRESOLVED` (the progress signal could not be read).
        waited_s: Seconds waited in total.
    """

    reason: str
    waited_s: float

    @property
    def done(self) -> bool:
        """Whether the awaited work completed."""
        return self.reason == DONE


def max_wait_s() -> float:
    """``arc.liveness.max_wait_s`` / ``CLIO_ARC_LIVENESS_MAX_WAIT_S`` (default 180 s)."""
    from clio_agent import conf  # noqa: PLC0415

    value = conf.resolve(
        "arc.liveness.max_wait_s",
        env="CLIO_ARC_LIVENESS_MAX_WAIT_S",
        default=_DEFAULT_MAX_WAIT_S,
        cast=conf.as_float,
    )
    return value if value > 0 else _DEFAULT_MAX_WAIT_S


def no_progress_window_s() -> float:
    """The no-progress window for ARC waits: the configured ``arc.liveness.stall_after_s``."""
    from clio_agent.arc.rpc_liveness import resolve_liveness_policy  # noqa: PLC0415 - cycle

    return resolve_liveness_policy().stall_after_s


def process_work(pid: int) -> float | None:
    """A process's work so far (CPU seconds + I/O MiB), ``None`` if it is gone.

    I/O counters are absent on macOS (``psutil.Process`` has no ``io_counters``) and can
    be denied for a foreign process; the CPU time alone is the work then.
    """
    import psutil  # noqa: PLC0415

    try:
        proc = psutil.Process(pid)
        times = proc.cpu_times()
    except psutil.Error:
        return None
    cpu = float(times.user + times.system)
    read_io = getattr(proc, "io_counters", None)
    if read_io is None:
        return cpu
    try:
        io = read_io()
    except psutil.NoSuchProcess:
        return None
    except psutil.AccessDenied:
        return cpu
    # Work = CPU seconds plus I/O (1 MiB counted as 1 "second"): a process flushing to a
    # slow disk is busy while its CPU time stays flat.
    return cpu + (io.read_bytes + io.write_bytes) / float(1 << 20)


class ProcessTreeWork:
    """Cumulative work of the process trees rooted at the pids added to it.

    Scoped to ONE wait: it remembers only the processes of the trees it measures, so
    its memory is bounded by that tree and released with the wait. A descendant that
    exits keeps the work it was last seen doing (a ``uv`` installer finishing must not
    read as "no progress" while the server it installed for is still starting).
    """

    def __init__(self, *roots: int) -> None:
        self._roots: list[int] = list(roots)
        self._seen: dict[tuple[int, float], float] = {}
        self._live: set[tuple[int, float]] = set()

    def add_root(self, pid: int) -> None:
        """Measure ``pid``'s tree too (an MCP server spawned for the awaited connect)."""
        if pid not in self._roots:
            self._roots.append(pid)

    @property
    def roots(self) -> tuple[int, ...]:
        """The root pids measured."""
        return tuple(self._roots)

    def sample(self) -> float | None:
        """The trees' cumulative work, ``None`` while there is no root to measure."""
        import psutil  # noqa: PLC0415

        if not self._roots:
            return None
        live: set[tuple[int, float]] = set()
        for root in self._roots:
            try:
                proc = psutil.Process(root)
                members = [proc, *proc.children(recursive=True)]
            except psutil.Error:
                continue
            for member in members:
                try:
                    key = (member.pid, member.create_time())
                except psutil.Error:
                    continue
                work = process_work(member.pid)
                if work is not None:
                    live.add(key)
                    self._seen[key] = max(work, self._seen.get(key, 0.0))
        # A member that exited since the last sample finished its work: progress, even
        # though the CPU it spent after the last sample can no longer be read.
        for key in self._live - live:
            self._seen[key] = self._seen.get(key, 0.0) + _EXIT_WORK
        self._live = live
        return sum(self._seen.values())


def daemon_work() -> float | None:
    """The clio-core daemon's work so far (CPU seconds + I/O MiB); ``None`` once it exited.

    Locates the daemon this process attached to: the config it attached with (so its
    port), then the pidfile or the port listener.

    Raises:
        DaemonPidUnresolved: No daemon process could be located.
    """
    import os  # noqa: PLC0415

    from clio_agent.arc import storage  # noqa: PLC0415 - lazy: storage is heavy
    from clio_agent.arc.clio_core_daemon import _resolve_daemon_pid  # noqa: PLC0415

    config_path = storage._active_config_path or os.environ.get("CLIO_SERVER_CONF", "")
    pid, _source = _resolve_daemon_pid(config_path, os.environ)
    if pid is None:
        raise DaemonPidUnresolved(
            f"no clio-core daemon process found (pidfile or listener; config={config_path!r})"
        )
    return process_work(pid)


def future_done_within(future: Future) -> Callable[[float], bool]:
    """``done_within`` for a ``concurrent.futures.Future``.

    Waits on an :class:`threading.Event` the future sets when it completes, created
    here, before the wait: each wait is then a plain lock wait that allocates nothing,
    so a caller parked in it while a native call holds the GIL shows a stack that
    ``faulthandler`` can walk (see :func:`wait_while_progressing`).
    """
    finished = threading.Event()
    future.add_done_callback(lambda _future: finished.set())
    return finished.wait


def _read_work(work: Callable[[], float | None]) -> tuple[float | None, bool]:
    """``(work, resolved)``: ``resolved`` is False when the daemon could not be located."""
    try:
        return work(), True
    except DaemonPidUnresolved:
        return None, False


def wait_while_progressing(
    done_within: Callable[[float], bool],
    *,
    slice_s: float,
    op_name: str,
    work: Callable[[], float | None] | None = None,
    ceiling_s: float | None = None,
    start: Callable[[], None] | None = None,
) -> WaitOutcome:
    """Wait (``done_within(slice_s)``) while ``work`` advances; the outcome says how it ended.

    ``work`` defaults to :func:`daemon_work`. A whole slice without an answer ends the
    wait as :data:`NO_PROGRESS` when the work did not advance (or the process is gone),
    :data:`DAEMON_PID_UNRESOLVED` when it could not be read, and :data:`CEILING` once the
    ceiling passed with the work still advancing.

    ``start``, when given, begins the awaited work AFTER the baseline work sample, so
    the baseline precedes the work and the caller goes straight from starting it to
    waiting. That ordering matters for a blocking native call started on a worker
    thread: the clio-core binding holds the GIL for the whole RPC, so a caller still
    sampling (``psutil`` reads, object construction) when the call takes the GIL is
    frozen mid-Python, where CPython 3.13's ``faulthandler`` cannot walk its stack (an
    ``__init__`` shim frame ends the dump at ``<invalid frame>``). Parked in its wait
    instead, its frames -- the caller's own code -- appear in any hang dump.
    """
    ceiling = ceiling_s if ceiling_s is not None else max_wait_s()
    read: Callable[[], Any] = work or (lambda: daemon_work())  # late-bound (patchable)
    started = time.monotonic()
    last, resolved = _read_work(read)
    if start is not None:
        start()
    while True:
        if done_within(slice_s):
            return WaitOutcome(DONE, time.monotonic() - started)
        waited = time.monotonic() - started
        current, now_resolved = _read_work(read)
        if not (resolved and now_resolved):
            logger.warning(
                "clio-core op=%s unanswered after %.0fs and its progress cannot be measured "
                "(reason=%s)",
                op_name,
                waited,
                DAEMON_PID_UNRESOLVED,
            )
            return WaitOutcome(DAEMON_PID_UNRESOLVED, waited)
        if current is None or (last is not None and current - last < _MIN_WORK_PROGRESS):
            return WaitOutcome(NO_PROGRESS, waited)
        if waited >= ceiling:
            logger.warning(
                "op=%s still unanswered after %.0fs although the awaited process is busy; "
                "giving up at the ceiling (reason=%s ceiling_s=%.0f)",
                op_name,
                waited,
                CEILING,
                ceiling,
            )
            return WaitOutcome(CEILING, waited)
        logger.info(
            "op=%s is slow to answer (%.0fs, awaited process busy: +%.2f work); waiting",
            op_name,
            waited,
            current - (last or 0.0),
        )
        last = current


def arc_write_work(completed: Callable[[], int]) -> Callable[[], float]:
    """Progress of a set of ARC writes: how many completed, plus the daemon's own work.

    For waits on a queue of ARC writes (the transcript minter drain, an in-flight
    invocation drain): a write completing is progress, and so is the daemon working
    while one write is still in flight. Without a locatable daemon (an in-memory
    store) only completions count.
    """

    def sample() -> float:
        try:
            daemon = daemon_work() or 0.0
        except DaemonPidUnresolved:
            daemon = 0.0
        return float(completed()) + daemon

    return sample


def drain_while_writes_progress(
    cv: threading.Condition,
    settled: Callable[[], bool],
    completed: Callable[[], int],
    *,
    op_name: str,
    no_progress_s: float | None = None,
) -> WaitOutcome:
    """Wait on ``cv`` until ``settled()`` while the ARC writes behind it keep progressing.

    ``completed`` counts the writes finished so far (read under ``cv``); together with
    the daemon's own work it is the progress signal (:func:`arc_write_work`). The
    no-progress window is ``no_progress_s`` (``arc.liveness.stall_after_s`` when
    ``None``), the ceiling ``arc.liveness.max_wait_s``.
    """

    def done_within(timeout: float) -> bool:
        with cv:
            return cv.wait_for(settled, timeout=timeout)

    def finished() -> int:
        with cv:
            return completed()

    return wait_while_progressing(
        done_within,
        slice_s=no_progress_s if no_progress_s is not None else no_progress_window_s(),
        op_name=op_name,
        work=arc_write_work(finished),
    )
