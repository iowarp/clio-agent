"""clio-core calls from short-lived threads run on a bounded set of process-lived threads.

clio-core keeps an epoll + signalfd for every OS thread that ever entered it; routing
short-lived callers through :mod:`clio_agent.arc.native_threads` keeps that per-thread
cost bounded by peak concurrency instead of growing with every thread a caller creates.
"""

from __future__ import annotations

import contextvars
import os
import sys
import threading
from pathlib import Path

import pytest

from clio_agent.arc.memory import ARCMemory
from clio_agent.arc.native_threads import native_thread_count, run_on_native_thread
from tests.test_arc.conftest import core_arc as core_arc

_REQUEST: contextvars.ContextVar[str] = contextvars.ContextVar("native_thread_test", default="")


def _on_new_thread(fn) -> object:
    box: list[object] = []
    thread = threading.Thread(target=lambda: box.append(fn()))
    thread.start()
    thread.join()
    return box[0]


def test_main_thread_calls_run_in_place() -> None:
    assert run_on_native_thread(threading.get_ident) == threading.get_ident()


def test_short_lived_callers_share_persistent_workers() -> None:
    before = native_thread_count()
    callers = set()
    runners = set()
    for _ in range(20):
        caller, runner = _on_new_thread(
            lambda: (threading.get_ident(), run_on_native_thread(threading.get_ident))
        )
        callers.add(caller)
        runners.add(runner)
    assert runner not in callers
    assert native_thread_count() - before <= 1  # sequential callers reuse one idle worker
    assert len(runners) <= max(native_thread_count(), 1)


def test_result_errors_and_context_cross_the_hand_off() -> None:
    def call() -> str:
        _REQUEST.set("caller")
        return run_on_native_thread(_REQUEST.get)

    assert _on_new_thread(call) == "caller"

    class Refused(RuntimeError):
        pass

    def failing() -> None:
        raise Refused("native refusal")

    def call_failing() -> object:
        try:
            run_on_native_thread(failing)
        except Refused as error:
            return error
        return None

    assert isinstance(_on_new_thread(call_failing), Refused)


def test_the_pool_grows_only_to_peak_concurrency() -> None:
    before = native_thread_count()
    entered = threading.Barrier(4)
    release = threading.Event()

    def blocked() -> None:
        entered.wait(5)
        release.wait(5)

    callers = [threading.Thread(target=lambda: run_on_native_thread(blocked)) for _ in range(3)]
    for caller in callers:
        caller.start()
    entered.wait(5)  # three calls are running at once, on three workers
    release.set()
    for caller in callers:
        caller.join(5)
    grown = native_thread_count() - before
    assert grown <= 3
    for _ in range(10):
        _on_new_thread(lambda: run_on_native_thread(lambda: None))
    assert native_thread_count() - before == grown  # idle workers serve later callers


def _runtime_descriptors() -> int:
    count = 0
    for fd in os.listdir("/proc/self/fd"):
        try:
            target = os.readlink(f"/proc/self/fd/{fd}")
        except OSError:
            continue
        count += "signalfd" in target or "eventpoll" in target
    return count


@pytest.mark.skipif(
    not sys.platform.startswith("linux") or not Path("/proc/self/fd").is_dir(),
    reason="clio-core's per-thread epoll/signalfd are observable through /proc on Linux",
)
def test_store_calls_from_new_threads_do_not_leak_runtime_descriptors(arc: ARCMemory) -> None:
    from clio_agent.arc import rpc_liveness

    def persistent_workers() -> int:
        return native_thread_count() + rpc_liveness._pool_threads_started

    store = arc._store
    store.put("probe", "warm", b"x")  # resolve the tag id; later calls take the inline path
    _on_new_thread(lambda: store.get("probe", "warm"))
    before = _runtime_descriptors()
    for index in range(20):
        _on_new_thread(lambda index=index: store.put("probe", f"t{index}", b"x"))
        assert _on_new_thread(lambda index=index: store.get("probe", f"t{index}")) is not None
    # 40 short-lived callers used to add a pair each (80). Now a pair belongs to a
    # process-lived worker on its first native call: bounded by the workers, not callers.
    assert _runtime_descriptors() - before <= 2 * persistent_workers()


@pytest.mark.skipif(
    not sys.platform.startswith("linux") or not Path("/proc/self/fd").is_dir(),
    reason="clio-core's per-thread epoll/signalfd are observable through /proc on Linux",
)
def test_opening_stores_on_new_threads_does_not_leak_runtime_descriptors(arc: ARCMemory) -> None:
    """A GACT turn opens its memory store on an AnyIO worker; workers come and go."""
    from clio_agent.arc import rpc_liveness
    from clio_agent.arc.storage import make_arc_store

    del arc  # the attached runtime and this test's namespace
    _on_new_thread(lambda: make_arc_store(backend="cte"))
    before = _runtime_descriptors()
    for _ in range(10):
        _on_new_thread(lambda: make_arc_store(backend="cte"))
    workers = native_thread_count() + rpc_liveness._pool_threads_started
    assert _runtime_descriptors() - before <= 2 * workers  # was 2 per thread: 20 here
