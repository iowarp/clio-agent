"""Run blocking work on a dedicated thread; wait while THAT thread keeps working.

A fixed wall-clock budget on an in-process computation (a table query over a large
artifact) turns a slow-but-working machine into a failure. The signal that separates a
query still crunching from one that is stuck is the CPU time of the one thread running
it: a working query keeps consuming CPU, a blocked one does not. Only that thread is
measured, never the whole server process, so unrelated busy work elsewhere in the
server can never make a stuck query look alive.

:func:`run_while_thread_works` starts the work on its own (named) thread, learns
the thread's OS identity from inside it, and waits in no-progress windows. After each
window without an answer it samples the thread's CPU seconds: advanced -> still working
(logged), otherwise :class:`ThreadStalled` (``no_progress``); a thread still working at
the ceiling is :class:`ThreadStalled` (``ceiling``). Nothing here falls back silently: a
thread whose CPU time cannot be read is :class:`ThreadWorkUnresolved`.

Per-thread CPU time comes from ``psutil.Process().threads()`` on Linux and Windows (the
ids are the kernel thread ids, :func:`threading.get_native_id`), and from the Mach
``thread_info`` call on macOS, where psutil reports thread indices instead of ids.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import ctypes
import logging
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")

#: CPU seconds the worker thread must add per window to count as progress.
MIN_THREAD_PROGRESS = 0.01

#: Typed reasons carried by :class:`ThreadStalled` (and logged).
REASON_NO_PROGRESS = "no_progress"
REASON_CEILING = "ceiling"


class ThreadStalled(TimeoutError):
    """The worker thread answered nothing and did no CPU work for a whole window.

    Attributes:
        op: What was awaited.
        reason: :data:`REASON_NO_PROGRESS` or :data:`REASON_CEILING`.
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
        return None  # not KERN_SUCCESS: the thread is gone
    user = info.user_time.seconds + info.user_time.microseconds / 1e6
    system = info.system_time.seconds + info.system_time.microseconds / 1e6
    return float(user + system)


def thread_cpu_seconds(identity: ThreadIdentity) -> float | None:
    """CPU seconds (user + system) the thread has used; ``None`` once it is gone.

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

    Cancelling the awaiting task, or a :class:`ThreadStalled`, stops the wait only;
    ``fn`` keeps running until it returns, so it should check its own cancellation
    signal (the caller sets it when it stops waiting).

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

    # One fresh single-worker pool per call: the work gets its own thread (whose CPU
    # time starts at zero), and the pool hands fn's result or exception to the caller.
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
        if current - last < MIN_THREAD_PROGRESS:
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
                REASON_CEILING,
                op,
                waited,
                ceiling_s,
            )
            raise ThreadStalled(op, REASON_CEILING, waited, no_progress_s)
        logger.info(
            "slow but working reason=still_working op=%s waited_s=%.0f cpu_s=+%.2f",
            op,
            waited,
            current - last,
        )
        last = current


__all__ = [
    "MIN_THREAD_PROGRESS",
    "REASON_CEILING",
    "REASON_NO_PROGRESS",
    "ThreadIdentity",
    "ThreadStalled",
    "ThreadWorkUnresolved",
    "current_thread_identity",
    "run_while_thread_works",
    "thread_cpu_seconds",
]
