"""A document process runs until it exits or is stopped; it is never cut off by a clock.

Each test runs a REAL child Python process so the wait, the kill of the whole tree and
the heartbeat are measured, not stubbed.
"""

from __future__ import annotations

import subprocess
import sys
import threading
import time
from pathlib import Path

import psutil
import pytest

from clio_agent.runtime.document_stack import process as document_process
from clio_agent.runtime.document_stack.process import (
    DocumentCancelled,
    DocumentError,
    run,
    wait_process,
    watched,
)


def _sleeper(seconds: float) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [sys.executable, "-c", f"import time; time.sleep({seconds}); print('done')"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def test_a_process_runs_past_any_old_cap_and_reports_that_it_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No timeout: the process finishes on its own and heartbeats show it still running.

    SABOTAGE: give ``run`` a default timeout again -> the job is killed early -> red.
    """
    monkeypatch.setattr(document_process, "HEARTBEAT_FIRST_S", 0.2)
    monkeypatch.setattr(document_process, "HEARTBEAT_EVERY_S", 0.2)
    beats: list[float] = []
    with watched(heartbeat=beats.append):
        output = run(
            [sys.executable, "-c", "import time; time.sleep(1.0); print('done')"],
            cwd=Path.cwd(),
        )
    assert output.strip() == "done"
    assert len(beats) >= 2 and beats == sorted(beats)


def test_a_stop_kills_the_whole_tree_and_raises_cancelled() -> None:
    """Stopping mid-run ends the process and its child, typed as cancelled, not failed."""
    script = (
        "import subprocess, sys, time;"
        "c = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']);"
        "print(c.pid, flush=True); time.sleep(60)"
    )
    parent = subprocess.Popen(
        [sys.executable, "-c", script], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    assert parent.stdout is not None
    child_pid = int(parent.stdout.readline())
    stop = threading.Event()
    threading.Timer(0.5, stop.set).start()
    started = time.monotonic()
    with pytest.raises(DocumentCancelled):
        wait_process(parent, name="slow", cancelled=stop.is_set)
    assert time.monotonic() - started < 30
    assert parent.poll() is not None
    deadline = time.monotonic() + 10
    while psutil.pid_exists(child_pid) and time.monotonic() < deadline:
        try:
            if psutil.Process(child_pid).status() == psutil.STATUS_ZOMBIE:
                break
        except psutil.Error:
            break
        time.sleep(0.1)
    try:
        alive = psutil.Process(child_pid).status() != psutil.STATUS_ZOMBIE
    except psutil.Error:
        alive = False
    assert not alive


def test_cancelled_is_a_document_error_and_an_explicit_timeout_still_applies() -> None:
    """Infrastructure steps may still pass a limit; agent jobs never do."""
    assert issubclass(DocumentCancelled, DocumentError)
    with pytest.raises(DocumentError, match="exceeded"):
        wait_process(_sleeper(30), name="install step", timeout=0.3)


def test_a_failing_heartbeat_never_stops_the_job(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(document_process, "HEARTBEAT_FIRST_S", 0.1)

    def broken(_elapsed: float) -> None:
        raise RuntimeError("watcher gone")

    stdout, _ = wait_process(_sleeper(0.6), name="job", heartbeat=broken)
    assert stdout.strip() == "done"
