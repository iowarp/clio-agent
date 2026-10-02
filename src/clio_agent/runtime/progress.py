"""Wait on work while it visibly progresses; never a fixed wall-clock deadline.

A slow machine and a hung peer look the same from the caller's side; only the awaited
work's own progress tells them apart. This module owns the work samplers -- a process
(:func:`process_work`), a process tree (:func:`tree_work`, :class:`ProcessTreeWork`) and
one thread (:func:`thread_cpu_seconds`) -- and the progress waits built on them:
:func:`run_probe` (a short CLI), :func:`await_while_working` (an awaitable) and
:func:`run_while_thread_works` (blocking work on its own thread). Each waits while the
work advances and fails typed after a whole window without progress or at the ceiling.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import ctypes
import logging
import subprocess
import sys
import threading
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any, TypeGuard, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")

#: The work (CPU seconds + I/O MiB) that must be added per window to count as progress.
MIN_PROGRESS = 0.01
#: The ceiling on waiting for work that keeps progressing but never answers.
DEFAULT_CEILING_S = 180.0
_EXIT_WORK = 0.1  # work credited for a tree member that exited between two samples

#: Typed reasons carried by the errors below (and logged).
REASON_NO_PROGRESS = "no_progress"
REASON_PROCESS_GONE = "process_gone"
REASON_CEILING = "ceiling_reached"
REASON_THREAD_CEILING = "ceiling"


def progressed(current: float | None, last: float | None) -> TypeGuard[float]:
    """Whether a work counter advanced: ``current`` readable and ``MIN_PROGRESS`` past ``last``."""
    return current is not None and (last is None or current - last >= MIN_PROGRESS)


# --- process work -------------------------------------------------------------------


def process_work(pid: int) -> float | None:
    """A process's work so far (CPU seconds + I/O MiB), ``None`` if it is gone.

    I/O counters are absent on macOS and can be denied for a foreign process; the CPU
    time alone is the work then.
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
    # 1 MiB of I/O counts as one CPU second: a process flushing to a slow disk is busy.
    return cpu + (io.read_bytes + io.write_bytes) / float(1 << 20)


def tree_work(pid: int) -> float | None:
    """Work of process ``pid`` and its live descendants; ``None`` if ``pid`` is gone."""
    import psutil  # noqa: PLC0415

    try:
        root = psutil.Process(pid)
        members = [root, *root.children(recursive=True)]
    except psutil.Error:
        return None
    return sum(work for proc in members if (work := process_work(proc.pid)) is not None)


class ProcessTreeWork:
    """Cumulative work of the process trees rooted at the pids added to it.

    Scoped to ONE wait, so its memory is bounded by the measured trees. A descendant
    that exits keeps the work it was last seen doing (a ``uv`` installer finishing must
    not read as "no progress" while the server it installed for is still starting).
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
        # A member that exited since the last sample finished its work: progress.
        for key in self._live - live:
            self._seen[key] = self._seen.get(key, 0.0) + _EXIT_WORK
        self._live = live
        return sum(self._seen.values())


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

    Distinct from a launch failure (``OSError``) so a slow CLI is never reported as
    missing or signed out.
    """

    def __init__(self, op: str, waited_s: float, reason: str) -> None:
        self.op = op
        self.waited_s = waited_s
        self.reason = reason
        super().__init__(
            f"{op} did not answer within {waited_s:.0f}s ({reason}); "
            "the program is installed but slow or unresponsive"
        )


def _stall_reason(current: float | None, last: float | None, waited: float, ceiling: float) -> str:
    """The typed reason to stop waiting after a silent window, or ``""`` to keep waiting."""
    if current is None:
        return REASON_PROCESS_GONE
    if not progressed(current, last):
        return REASON_NO_PROGRESS
    return REASON_CEILING if waited >= ceiling else ""


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
            if proc.poll() is not None:
                continue  # it exited just now: the next communicate() collects its output
            waited = time.monotonic() - started
            work = tree_work(proc.pid)
            reason = _stall_reason(work, last_work, waited, ceiling_s)
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
            reason = _stall_reason(current, last_work, waited, ceiling_s)
            if reason:
                logger.warning("wait abandoned reason=%s op=%s waited_s=%.0f", reason, op, waited)
                raise NoProgressError(op, waited, reason)
            logger.info("slow but working reason=still_working op=%s waited_s=%.0f", op, waited)
            last_work = current
            wait = stretch_s
    finally:
        if not task.done():
            task.cancel()


# --- thread work --------------------------------------------------------------------


class ThreadStalled(TimeoutError):
    """The worker thread answered nothing and did no CPU work for a whole window.

    Attributes:
        op: What was awaited.
        reason: :data:`REASON_NO_PROGRESS` or :data:`REASON_THREAD_CEILING`.
        waited_s: Seconds waited in total.
        window_s: The no-progress window.
    """

    def __init__(self, op: str, reason: str, waited_s: float, window_s: float) -> None:
        self.op = op
        self.reason = reason
        self.waited_s = waited_s
        self.window_s = window_s
        what = (
            f"no answer and no CPU work for {window_s:g}s"
            if reason == REASON_NO_PROGRESS
            else "still working at the ceiling"
        )
        super().__init__(f"{op}: {what} (waited {waited_s:.0f}s, reason={reason})")


class ThreadWorkUnresolved(RuntimeError):
    """The worker thread's CPU time could not be read on this platform/process."""


@dataclass(frozen=True)
class ThreadIdentity:
    """How to find one live thread's CPU time from another thread.

    Attributes:
        native_id: The kernel thread id (:func:`threading.get_native_id`).
        mach_port: The Mach thread port (macOS only; ``None`` elsewhere).
    """

    native_id: int
    mach_port: int | None = None


class _TimeValue(ctypes.Structure):
    _fields_ = [("seconds", ctypes.c_int), ("microseconds", ctypes.c_int)]


class _ThreadBasicInfo(ctypes.Structure):
    _fields_ = [
        ("user_time", _TimeValue),
        ("system_time", _TimeValue),
        ("cpu_usage", ctypes.c_int),
        ("policy", ctypes.c_int),
        ("run_state", ctypes.c_int),
        ("flags", ctypes.c_int),
        ("suspend_count", ctypes.c_int),
        ("sleep_time", ctypes.c_int),
    ]


_THREAD_BASIC_INFO = 3  # <mach/thread_info.h> THREAD_BASIC_INFO flavor
_KERN_SUCCESS = 0


def _libsystem() -> ctypes.CDLL:
    return ctypes.CDLL("/usr/lib/libSystem.B.dylib")


def current_thread_identity() -> ThreadIdentity:
    """The calling thread's identity (call it FROM the thread to be measured)."""
    native_id = threading.get_native_id()
    if sys.platform != "darwin":
        return ThreadIdentity(native_id)
    lib = _libsystem()
    lib.pthread_self.restype = ctypes.c_void_p
    lib.pthread_mach_thread_np.argtypes = [ctypes.c_void_p]
    lib.pthread_mach_thread_np.restype = ctypes.c_uint
    return ThreadIdentity(native_id, int(lib.pthread_mach_thread_np(lib.pthread_self())))


def _mach_thread_cpu_seconds(port: int) -> float | None:
    lib = _libsystem()
    lib.thread_info.argtypes = [
        ctypes.c_uint,
        ctypes.c_int,
        ctypes.POINTER(_ThreadBasicInfo),
        ctypes.POINTER(ctypes.c_uint),
    ]
    lib.thread_info.restype = ctypes.c_int
    info = _ThreadBasicInfo()
    count = ctypes.c_uint(ctypes.sizeof(_ThreadBasicInfo) // ctypes.sizeof(ctypes.c_uint))
    status = lib.thread_info(port, _THREAD_BASIC_INFO, ctypes.byref(info), ctypes.byref(count))
    if status != _KERN_SUCCESS:
        return None  # the thread is gone
    user = info.user_time.seconds + info.user_time.microseconds / 1e6
    system = info.system_time.seconds + info.system_time.microseconds / 1e6
    return float(user + system)


def thread_cpu_seconds(identity: ThreadIdentity) -> float | None:
    """CPU seconds (user + system) the thread has used; ``None`` once it is gone.

    psutil reports per-thread times by kernel id on Linux and Windows; macOS needs the
    Mach ``thread_info`` call (psutil reports thread indices there).

    Raises:
        ThreadWorkUnresolved: This platform/process gives no per-thread CPU time.
    """
    if sys.platform == "darwin":
        if identity.mach_port is None:
            raise ThreadWorkUnresolved("macOS thread identity has no Mach port")
        return _mach_thread_cpu_seconds(identity.mach_port)
    import psutil  # noqa: PLC0415

    try:
        threads = psutil.Process().threads()
    except psutil.Error as exc:
        raise ThreadWorkUnresolved(f"per-thread CPU times are unreadable: {exc!r}") from exc
    for entry in threads:
        if entry.id == identity.native_id:
            return float(entry.user_time + entry.system_time)
    return None


async def run_while_thread_works(
    fn: Callable[[], T],
    *,
    op: str,
    no_progress_s: float,
    ceiling_s: float,
    thread_name: str,
) -> T:
    """Run ``fn`` on a dedicated thread; wait while that thread's CPU time advances.

    Only that thread is measured, so unrelated busy work elsewhere in the process never
    makes a stuck ``fn`` look alive. Cancelling the awaiting task, or a
    :class:`ThreadStalled`, stops the wait only; ``fn`` keeps running until it returns,
    so it should check its own cancellation signal.

    Raises:
        ThreadStalled: A whole ``no_progress_s`` window without an answer and without
            CPU work, or still working at ``ceiling_s``.
        ThreadWorkUnresolved: The thread's CPU time could not be read.
        BaseException: Whatever ``fn`` raised.
    """
    identity: list[ThreadIdentity] = []

    def _measured() -> T:
        identity.append(current_thread_identity())
        return fn()

    # A fresh single-worker pool per call: the work gets its own thread (CPU time from zero).
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix=thread_name)
    result = pool.submit(_measured)
    pool.shutdown(wait=False)
    awaited = asyncio.wrap_future(result)
    started = time.monotonic()
    last = 0.0  # a fresh thread starts at zero CPU time
    while True:
        done, _pending = await asyncio.wait({awaited}, timeout=no_progress_s)
        if done or result.done():
            return result.result()
        waited = time.monotonic() - started
        current = thread_cpu_seconds(identity[0]) if identity else last
        if current is None:  # thread exited: its result is already set
            return result.result()
        if not progressed(current, last):
            logger.warning(
                "wait abandoned reason=%s op=%s waited_s=%.0f window_s=%g",
                REASON_NO_PROGRESS,
                op,
                waited,
                no_progress_s,
            )
            raise ThreadStalled(op, REASON_NO_PROGRESS, waited, no_progress_s)
        if waited >= ceiling_s:
            logger.warning(
                "wait abandoned reason=%s op=%s waited_s=%.0f ceiling_s=%g",
                REASON_THREAD_CEILING,
                op,
                waited,
                ceiling_s,
            )
            raise ThreadStalled(op, REASON_THREAD_CEILING, waited, no_progress_s)
        logger.info(
            "slow but working reason=still_working op=%s waited_s=%.0f cpu_s=+%.2f",
            op,
            waited,
            current - last,
        )
        last = current
