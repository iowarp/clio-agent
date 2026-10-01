"""A slow but working clio-core daemon is waited for; a hung or missing one is a stall.

A fixed wall-clock bound turned a slow-but-healthy daemon (a first run on a laptop, a
busy test suite) into a hard failure. The wait now follows the daemon's own CPU time.
"""

from __future__ import annotations

import threading
from concurrent.futures import Future

from clio_agent.arc.daemon_progress import future_done_within, wait_while_progressing


def _answer_after(seconds: float) -> Future:
    fut: Future = Future()
    threading.Timer(seconds, lambda: fut.set_result("ok")).start()
    return fut


def _cpu_counter(step: float):
    state = {"cpu": 0.0}

    def read() -> float:
        state["cpu"] += step
        return state["cpu"]

    return read


def test_a_busy_daemon_is_waited_for_past_the_slice() -> None:
    fut = _answer_after(0.5)
    assert wait_while_progressing(
        future_done_within(fut), slice_s=0.1, op_name="get", cpu_seconds=_cpu_counter(0.5)
    )


def test_a_daemon_whose_cpu_does_not_advance_is_a_stall() -> None:
    fut = _answer_after(5.0)
    assert not wait_while_progressing(
        future_done_within(fut), slice_s=0.1, op_name="get", cpu_seconds=lambda: 7.0
    )


def test_a_missing_daemon_is_a_stall() -> None:
    fut = _answer_after(5.0)
    assert not wait_while_progressing(
        future_done_within(fut), slice_s=0.1, op_name="get", cpu_seconds=lambda: None
    )


def test_even_a_busy_daemon_stops_at_the_ceiling() -> None:
    fut = _answer_after(5.0)
    assert not wait_while_progressing(
        future_done_within(fut),
        slice_s=0.1,
        op_name="get",
        cpu_seconds=_cpu_counter(0.5),
        ceiling_s=0.3,
    )


def test_a_prompt_answer_returns_at_once() -> None:
    fut: Future = Future()
    fut.set_result("ok")
    assert wait_while_progressing(
        future_done_within(fut), slice_s=10.0, op_name="get", cpu_seconds=lambda: None
    )
