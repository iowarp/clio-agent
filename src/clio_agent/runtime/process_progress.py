"""Wait on one process tree while it visibly works; a slow machine is never "absent".

A fixed wall-clock bound on a CLI probe (``claude --version``, ``claude auth status``,
``codex --version``) or on the Claude Code CLI's connect turns a slow host into a wrong
verdict: "not installed", "signed out", "timed out". The signal that separates a slow
process from a hung one is the awaited process tree's own work -- CPU seconds plus the
bytes it read or wrote (where the platform reports I/O; macOS does not). Only that tree is
measured, never every descendant of this server, so an unrelated busy child can never
make a hung probe look alive.

:func:`run_probe` (sync, a short-lived CLI) and :func:`await_while_working` (async, any
awaitable plus a work signal) both answer fast when the work answers fast, keep waiting
while the tree works, and fail **typed** -- :class:`ProbeUnresponsiveError` /
:class:`NoProgressError` naming the operation, the time waited and why -- after a whole
stretch with no progress or at the ceiling. Nothing here falls back silently.
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
import time
from collections.abc import Awaitable, Callable, Sequence
from typing import Any, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")

#: The work (CPU seconds + I/O MiB) a tree must add per stretch to count as progress.
MIN_PROGRESS = 0.01
#: The ceiling on waiting for a tree that keeps working but never answers.
DEFAULT_CEILING_S = 180.0

#: Typed reasons carried by the errors below (and logged).
REASON_NO_PROGRESS = "no_progress"
REASON_PROCESS_GONE = "process_gone"
REASON_CEILING = "ceiling_reached"


class NoProgressError(TimeoutError):
    """An awaited operation answered nothing and its process tree did no work.

    ``reason`` is :data:`REASON_NO_PROGRESS`, :data:`REASON_PROCESS_GONE` or
    :data:`REASON_CEILING`; ``waited_s`` is how long it was waited for.
    """

    def __init__(self, op: str, waited_s: float, reason: str) -> None:
        self.op = op
        self.waited_s = waited_s
        self.reason = reason
        super().__init__(f"{op}: no answer after {waited_s:.0f}s ({reason})")


class ProbeUnresponsiveError(RuntimeError):
    """A CLI probe ran but did not answer: it is installed, but slow or unresponsive.

    Distinct from a launch failure (``OSError``: the binary is absent or not runnable)
    so a caller never reports a slow CLI as missing or signed out.
    """

    def __init__(self, op: str, waited_s: float, reason: str) -> None:
        self.op = op
        self.waited_s = waited_s
        self.reason = reason
        super().__init__(
            f"{op} did not answer within {waited_s:.0f}s ({reason}); "
            "the program is installed but slow or unresponsive"
        )


def tree_work(pid: int) -> float | None:
    """CPU seconds + I/O MiB of process ``pid`` and its live descendants; ``None`` if gone.

    I/O counts where psutil reports it (Linux, Windows); on macOS ``io_counters`` does
    not exist and CPU time alone is the signal.
    """
    import psutil  # noqa: PLC0415

    try:
        root = psutil.Process(pid)
        members = [root, *root.children(recursive=True)]
    except psutil.Error:
        return None
    total = 0.0
    for proc in members:
        try:
            times = proc.cpu_times()
            total += float(times.user + times.system)
            io_counters = getattr(proc, "io_counters", None)
            if io_counters is not None:
                io = io_counters()
                total += (io.read_bytes + io.write_bytes) / float(1 << 20)
        except psutil.Error:
            continue  # a descendant that exited mid-walk: the rest still count
    return total


def run_probe(
    argv: Sequence[str],
    *,
    op: str,
    first_wait_s: float,
    stretch_s: float | None = None,
    ceiling_s: float = DEFAULT_CEILING_S,
    **popen_kwargs: Any,
) -> subprocess.CompletedProcess[str]:
    """Run a short CLI probe; wait past ``first_wait_s`` only while it keeps working.

    Args:
        argv: The command.
        op: The operation name for logs and the typed error.
        first_wait_s: The usual answer time; a probe answering within it costs nothing extra.
        stretch_s: How long a no-progress stretch may last after that (default
            ``first_wait_s``).
        ceiling_s: The longest the probe is waited for while it keeps working.
        **popen_kwargs: Extra ``subprocess.Popen`` arguments (e.g. ``creationflags``).

    Returns:
        The completed process (text mode, stdout/stderr captured).

    Raises:
        OSError: The program could not be launched (absent / not executable).
        ProbeUnresponsiveError: It launched but answered nothing and did no work for a
            whole stretch, or kept working past ``ceiling_s``. The probe is killed.
    """
    started = time.monotonic()
    proc = subprocess.Popen(  # noqa: S603 - fixed argv supplied by the caller (a discovered CLI)
        list(argv),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        **popen_kwargs,
    )
    stretch = stretch_s if stretch_s is not None else first_wait_s
    last_work = tree_work(proc.pid) or 0.0
    wait = first_wait_s
    try:
        while True:
            try:
                stdout, stderr = proc.communicate(timeout=wait)
            except subprocess.TimeoutExpired:
                pass
            else:
                return subprocess.CompletedProcess(list(argv), proc.returncode, stdout, stderr)
            waited = time.monotonic() - started
            work = tree_work(proc.pid)
            reason = ""
            if work is None:
                reason = REASON_PROCESS_GONE
            elif work - last_work < MIN_PROGRESS:
                reason = REASON_NO_PROGRESS
            elif waited >= ceiling_s:
                reason = REASON_CEILING
            if reason:
                logger.warning(
                    "probe unresponsive reason=probe_%s op=%s waited_s=%.0f", reason, op, waited
                )
                raise ProbeUnresponsiveError(op, waited, reason)
            logger.info(
                "probe slow but working reason=probe_still_working op=%s waited_s=%.0f",
                op,
                waited,
            )
            last_work = work if work is not None else last_work
            wait = stretch
    finally:
        if proc.poll() is None:
            _kill_tree(proc)


def _kill_tree(proc: subprocess.Popen[str]) -> None:
    """Kill an abandoned probe and its descendants (a ``.cmd`` shim's child holds the pipes)."""
    import psutil  # noqa: PLC0415

    try:
        children = psutil.Process(proc.pid).children(recursive=True)
    except psutil.Error:
        children = []
    for child in children:
        try:
            child.kill()
        except psutil.Error:
            continue  # already gone
    proc.kill()
    proc.communicate()


async def await_while_working(
    awaitable: Awaitable[T],
    *,
    op: str,
    work: Callable[[], float | None],
    first_wait_s: float,
    stretch_s: float,
    ceiling_s: float = DEFAULT_CEILING_S,
) -> T:
    """Await ``awaitable`` while ``work()`` (the awaited tree's work) keeps advancing.

    The first ``first_wait_s`` is free; after it, each ``stretch_s`` without an answer must
    show progress in ``work()`` (``None`` = the tree is gone). A stretch without progress,
    or reaching ``ceiling_s``, cancels the awaitable and raises :class:`NoProgressError`.
    """
    task = asyncio.ensure_future(awaitable)
    started = time.monotonic()
    last_work = work()
    wait = first_wait_s
    try:
        while True:
            done, _pending = await asyncio.wait({task}, timeout=wait)
            if done:
                return task.result()
            waited = time.monotonic() - started
            current = work()
            reason = ""
            if current is None:
                reason = REASON_PROCESS_GONE
            elif last_work is not None and current - last_work < MIN_PROGRESS:
                reason = REASON_NO_PROGRESS
            elif waited >= ceiling_s:
                reason = REASON_CEILING
            if reason:
                logger.warning("wait abandoned reason=%s op=%s waited_s=%.0f", reason, op, waited)
                raise NoProgressError(op, waited, reason)
            logger.info("slow but working reason=still_working op=%s waited_s=%.0f", op, waited)
            last_work = current
            wait = stretch_s
    finally:
        if not task.done():
            task.cancel()


__all__ = [
    "DEFAULT_CEILING_S",
    "NoProgressError",
    "ProbeUnresponsiveError",
    "REASON_CEILING",
    "REASON_NO_PROGRESS",
    "REASON_PROCESS_GONE",
    "await_while_working",
    "run_probe",
    "tree_work",
]
