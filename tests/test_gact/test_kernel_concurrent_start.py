"""Two scripts from one agent at once share one kernel, even while it is starting.

Live 2026-11-19: parallel ``run_code`` calls from one researcher logged "kernel started"
twice for the same scope, and one call failed within milliseconds ("the Python kernel did
not start" / "did not take its tools" / "stopped"): the second call found the first's
kernel not yet alive, took it for dead and replaced it mid-start.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from clio_agent.gact.code_mode import kernels


def _slow_starts(monkeypatch: pytest.MonkeyPatch, *, fail: bool = False) -> list[Any]:
    """Stand in for the kernel process: each start takes 0.3 s; records starts and stops."""

    events: list[Any] = []

    def start(self: kernels.Kernel) -> None:
        events.append(("start", id(self)))
        time.sleep(0.3)
        if fail:
            raise kernels.KernelError("the Python kernel did not start: test")
        self.process = SimpleNamespace(poll=lambda: None)

    def stop(self: kernels.Kernel, reason: str) -> None:
        events.append(("stop", id(self), reason))
        self.process = SimpleNamespace(poll=lambda: 0)

    monkeypatch.setattr(kernels.Kernel, "start", start)
    monkeypatch.setattr(kernels.Kernel, "stop", stop)
    monkeypatch.setattr(kernels, "_KERNELS", {})
    monkeypatch.setattr(kernels, "_start_reaper", lambda: None)
    return events


def _together(tmp_path: Path) -> list[Any]:
    results: list[Any] = []

    def call() -> None:
        try:
            results.append(kernels.kernel_for("s1", "researcher", tmp_path))
        except kernels.KernelError as error:
            results.append(error)

    threads = [threading.Thread(target=call) for _ in range(2)]
    threads[0].start()
    time.sleep(0.05)  # the second arrives while the first kernel is starting
    threads[1].start()
    for thread in threads:
        thread.join()
    return results


def test_a_second_script_waits_for_the_starting_kernel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events = _slow_starts(monkeypatch)
    first, second = _together(tmp_path)
    assert first is second
    assert [event[0] for event in events] == ["start"]


def test_a_failed_start_fails_both_scripts_and_the_next_one_starts_afresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events = _slow_starts(monkeypatch, fail=True)
    results = _together(tmp_path)
    assert all(isinstance(result, kernels.KernelError) for result in results)
    assert [event[0] for event in events] == ["start"]
    monkeypatch.setattr(
        kernels.Kernel,
        "start",
        lambda self: setattr(self, "process", SimpleNamespace(poll=lambda: None)),
    )
    fresh = kernels.kernel_for("s1", "researcher", tmp_path)
    assert fresh.alive()
