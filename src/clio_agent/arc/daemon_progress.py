"""Wait for a clio-core answer while the daemon is visibly working; never a fixed deadline.

A slow machine (or a busy daemon) answers late; a zombie daemon never answers. From the
caller's side both look the same, so a fixed wall-clock bound turns a slow-but-healthy
daemon into a failure -- on a first run, on a laptop, under a test suite. The signal
that tells them apart is the daemon's own CPU time: a daemon working through a backlog
keeps consuming CPU or doing I/O, a hung one does neither.

:func:`wait_while_progressing` waits in slices; after each slice without an answer it
checks the daemon process: alive and its CPU time advanced -> still working, keep
waiting (logged); otherwise -> a stall. A long configurable ceiling
(``arc.liveness.max_wait_s``) bounds even a busy daemon that never answers, so nothing
hangs forever.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from concurrent.futures import Future
from concurrent.futures import wait as futures_wait

logger = logging.getLogger(__name__)

_MIN_CPU_PROGRESS_S = 0.01  # the daemon's CPU time must advance by at least this per slice
_DEFAULT_MAX_WAIT_S = 600.0


def max_wait_s() -> float:
    """``arc.liveness.max_wait_s`` / ``CLIO_ARC_LIVENESS_MAX_WAIT_S`` (default 600 s)."""
    from clio_agent import conf  # noqa: PLC0415

    value = conf.resolve(
        "arc.liveness.max_wait_s",
        env="CLIO_ARC_LIVENESS_MAX_WAIT_S",
        default=_DEFAULT_MAX_WAIT_S,
        cast=conf.as_float,
    )
    return value if value > 0 else _DEFAULT_MAX_WAIT_S


def process_work(pid: int) -> float | None:
    """A process's work so far (CPU seconds + I/O MiB), ``None`` if it is gone."""
    import psutil  # noqa: PLC0415

    try:
        proc = psutil.Process(pid)
        times = proc.cpu_times()
        io = proc.io_counters()
    except psutil.Error:
        return None
    # Work = CPU seconds plus I/O (1 MiB counted as 1 "second"): a daemon flushing to a
    # slow disk is busy while its CPU time stays flat.
    io_mib = (io.read_bytes + io.write_bytes) / float(1 << 20)
    return float(times.user + times.system) + io_mib


def descendants_work() -> float:
    """Total work (CPU seconds + I/O MiB) of this process's descendants -- the MCP servers
    and their launchers (uv installing, Python importing) run there."""
    import psutil  # noqa: PLC0415

    total = 0.0
    for child in psutil.Process().children(recursive=True):
        work = process_work(child.pid)
        if work is not None:
            total += work
    return total


def daemon_cpu_seconds() -> float | None:
    """The clio-core daemon's work so far (CPU seconds + I/O MiB), ``None`` if none is found."""
    from clio_agent.arc.clio_core_daemon import _resolve_daemon_pid  # noqa: PLC0415

    pid, _source = _resolve_daemon_pid("", None)
    return None if pid is None else process_work(pid)


def future_done_within(future: Future) -> Callable[[float], bool]:
    """``done_within`` for a ``concurrent.futures.Future``."""
    return lambda timeout: future in futures_wait([future], timeout=timeout)[0]


def wait_while_progressing(
    done_within: Callable[[float], bool],
    *,
    slice_s: float,
    op_name: str,
    cpu_seconds: Callable[[], float | None] | None = None,
    ceiling_s: float | None = None,
) -> bool:
    """Wait (``done_within(slice_s)``) while the daemon makes progress; ``True`` once done.

    Returns ``False`` (a stall) when a whole slice passes with the daemon gone or its CPU
    time not advancing, or when the ceiling is reached.
    """
    ceiling = ceiling_s if ceiling_s is not None else max_wait_s()
    cpu_seconds = cpu_seconds or (lambda: daemon_cpu_seconds())  # late-bound (patchable)
    started = time.monotonic()
    last_cpu = cpu_seconds()
    while True:
        if done_within(slice_s):
            return True
        waited = time.monotonic() - started
        cpu = cpu_seconds()
        if cpu is None or last_cpu is None or cpu - last_cpu < _MIN_CPU_PROGRESS_S:
            return False
        if waited >= ceiling:
            logger.warning(
                "clio-core op=%s still unanswered after %.0fs although the daemon is busy; "
                "giving up at the ceiling (arc.liveness.max_wait_s=%.0f)",
                op_name,
                waited,
                ceiling,
            )
            return False
        logger.info(
            "clio-core is slow to answer op=%s (%.0fs, daemon busy: +%.2fs CPU); waiting",
            op_name,
            waited,
            cpu - last_cpu,
        )
        last_cpu = cpu
