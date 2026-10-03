"""A slow but working peer is waited for; a hung, missing or unlocatable one ends typed.

A fixed wall-clock bound turned a slow-but-healthy daemon (a first run on a laptop, a
busy test suite) into a hard failure. The wait follows the awaited process's own work,
and only that process's: an unrelated busy child of this process (the clio_run daemon
is one) never makes a hung MCP server look busy.
"""

from __future__ import annotations

import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from concurrent.futures import Future

import psutil
import pytest

from clio_agent.arc import daemon_progress
from clio_agent.arc.daemon_progress import (
    CEILING,
    DAEMON_PID_UNRESOLVED,
    DONE,
    NO_PROGRESS,
    DaemonPidUnresolved,
    future_done_within,
    wait_while_progressing,
)
from clio_agent.runtime.progress import ProcessTreeWork, process_work

_BURN = (
    "import sys, time\n"
    "end = time.monotonic() + float(sys.argv[1])\n"
    "while time.monotonic() < end:\n"
    "    pass\n"
    "print('done', flush=True)\n"
    "sys.stdin.readline()\n"
)
# Burns a fixed amount of CPU time (not wall time): on a loaded CI runner a wall-clock
# burn got 0.09 CPU-s of its 0.5 s, so an assertion on the work done went red (CI).
_BURN_CPU = (
    "import sys, time\n"
    "end = time.process_time() + float(sys.argv[1])\n"
    "while time.process_time() < end:\n"
    "    pass\n"
    "print('done', flush=True)\n"
    "sys.stdin.readline()\n"
)
_IDLE = "import sys; print('ready', flush=True); sys.stdin.readline()\n"


def _answer_after(seconds: float) -> Future:
    fut: Future = Future()
    threading.Timer(seconds, lambda: fut.set_result("ok")).start()
    return fut


def _counter(step: float):
    state = {"w": 0.0}

    def read() -> float:
        state["w"] += step
        return state["w"]

    return read


@pytest.fixture
def child_procs() -> Iterator[list[subprocess.Popen[str]]]:
    procs: list[subprocess.Popen[str]] = []
    yield procs
    for proc in procs:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=10)


def _spawn(procs: list[subprocess.Popen[str]], source: str, *args: str) -> subprocess.Popen[str]:
    proc = subprocess.Popen(
        [sys.executable, "-c", source, *args],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    procs.append(proc)
    return proc


def test_a_busy_peer_is_waited_for_past_the_slice() -> None:
    outcome = wait_while_progressing(
        future_done_within(_answer_after(0.5)), slice_s=0.1, op_name="get", work=_counter(0.5)
    )
    assert outcome.reason == DONE and outcome.done


def test_a_peer_whose_work_does_not_advance_is_no_progress() -> None:
    outcome = wait_while_progressing(
        future_done_within(_answer_after(5.0)), slice_s=0.1, op_name="get", work=lambda: 7.0
    )
    assert outcome.reason == NO_PROGRESS


def test_a_gone_peer_is_no_progress() -> None:
    outcome = wait_while_progressing(
        future_done_within(_answer_after(5.0)), slice_s=0.1, op_name="get", work=lambda: None
    )
    assert outcome.reason == NO_PROGRESS


def test_even_a_busy_peer_stops_at_the_ceiling() -> None:
    outcome = wait_while_progressing(
        future_done_within(_answer_after(5.0)),
        slice_s=0.1,
        op_name="get",
        work=_counter(0.5),
        ceiling_s=0.3,
    )
    assert outcome.reason == CEILING


def test_a_prompt_answer_returns_at_once() -> None:
    fut: Future = Future()
    fut.set_result("ok")
    outcome = wait_while_progressing(
        future_done_within(fut), slice_s=10.0, op_name="get", work=lambda: None
    )
    assert outcome.done


def test_start_runs_after_the_baseline_sample() -> None:
    """``start`` begins the awaited work after the baseline sample, then the wait begins."""
    events: list[str] = []
    fut: Future = Future()

    def work() -> float:
        events.append("sample")
        return float(len(events))

    def start() -> None:
        events.append("start")
        fut.set_result("ok")

    outcome = wait_while_progressing(
        future_done_within(fut), slice_s=5.0, op_name="get", work=work, start=start
    )
    assert outcome.done
    assert events == ["sample", "start"]


def test_future_done_within_waits_for_a_later_answer() -> None:
    done_within = future_done_within(_answer_after(0.2))
    assert not done_within(0.01)
    assert done_within(5.0)


def test_an_unlocatable_daemon_is_typed_unresolved_not_a_stall() -> None:
    """``_resolve_daemon_pid`` finding nothing is ``daemon_pid_unresolved``, never ``no_progress``.

    **Sabotage:** treat :class:`DaemonPidUnresolved` like a gone process -> ``no_progress``.
    """

    def unresolved() -> float:
        raise DaemonPidUnresolved("no pidfile, no listener")

    outcome = wait_while_progressing(
        future_done_within(_answer_after(5.0)), slice_s=0.1, op_name="get", work=unresolved
    )
    assert outcome.reason == DAEMON_PID_UNRESOLVED


def test_daemon_work_resolves_with_the_attached_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """The daemon is located with the config this process attached with (its real port),
    not ``("", None)``; nothing located raises :class:`DaemonPidUnresolved`."""
    from clio_agent.arc import clio_core_daemon, storage

    seen: list[str] = []

    def resolve(config_path: str, env: object) -> tuple[int | None, str]:
        seen.append(config_path)
        return None, ""

    monkeypatch.setattr(storage, "_active_config_path", "D:/cfg/attached.yaml")
    monkeypatch.setattr(clio_core_daemon, "_resolve_daemon_pid", resolve)
    with pytest.raises(DaemonPidUnresolved):
        daemon_progress.daemon_work()
    assert seen == ["D:/cfg/attached.yaml"]


def test_process_work_without_io_counters_is_cpu_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """macOS: ``psutil.Process`` has no ``io_counters``; the CPU time is the work.

    **Sabotage:** call ``proc.io_counters()`` unguarded -> ``AttributeError`` escapes.
    """
    monkeypatch.delattr(psutil.Process, "io_counters", raising=False)
    work = process_work(psutil.Process().pid)
    assert work is not None and work > 0


def test_tree_work_measures_only_the_awaited_tree(
    child_procs: list[subprocess.Popen[str]],
) -> None:
    """A busy sibling (the clio_run daemon is a psutil child of this process too) never
    makes an idle awaited server look busy.

    **Sabotage:** sum every descendant of this process (the old ``descendants_work``) ->
    the busy sibling's CPU reads as the server's progress.
    """
    idle_server = _spawn(child_procs, _IDLE)
    assert idle_server.stdout is not None and idle_server.stdout.readline().strip() == "ready"
    _spawn(child_procs, _BURN, "30")  # a busy, unrelated child: a stand-in daemon

    tree = ProcessTreeWork(idle_server.pid)
    before = tree.sample()
    time.sleep(0.6)
    after = tree.sample()

    assert before is not None and after is not None
    assert after - before < 0.05


def test_tree_work_is_cumulative_when_a_working_child_exits(
    child_procs: list[subprocess.Popen[str]],
) -> None:
    """A finished descendant (a ``uv`` installer) keeps the work it did: never a dip."""
    launcher = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import subprocess, sys\n"
            f"p = subprocess.Popen([sys.executable, '-c', {_BURN_CPU!r}, '0.5'],"
            " stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)\n"
            "print(p.stdout.readline().strip(), flush=True)\n"
            "sys.stdin.readline()\n"
            "p.stdin.write('\\n'); p.stdin.flush(); p.wait()\n"
            "print('child-exited', flush=True)\n"
            "sys.stdin.readline()\n",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    child_procs.append(launcher)
    assert launcher.stdout is not None
    tree = ProcessTreeWork(launcher.pid)
    assert launcher.stdout.readline().strip() == "done"
    before_exit = tree.sample()
    # Keep the descendant alive until sampled, including on a busy CI runner.
    assert launcher.stdin is not None
    launcher.stdin.write("\n")
    launcher.stdin.flush()
    assert launcher.stdout.readline().strip() == "child-exited"
    after_exit = tree.sample()

    assert before_exit is not None and before_exit >= 0.4
    assert after_exit is not None and after_exit >= before_exit


def test_a_tree_with_no_root_is_unmeasurable() -> None:
    assert ProcessTreeWork().sample() is None
