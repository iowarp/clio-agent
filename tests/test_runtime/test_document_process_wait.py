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
    with watched(heartbeat=lambda elapsed, _stalled: beats.append(elapsed)):
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

    def broken(_elapsed: float, _stalled: float | None) -> None:
        raise RuntimeError("watcher gone")

    stdout, _ = wait_process(_sleeper(0.6), name="job", heartbeat=broken)
    assert stdout.strip() == "done"


def test_a_heartbeat_bug_ends_the_job_with_its_tree(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only the watcher's delivery failures are survived; a bug surfaces, never orphaning."""
    monkeypatch.setattr(document_process, "HEARTBEAT_FIRST_S", 0.1)

    def buggy(_elapsed: float, _stalled: float | None) -> None:
        raise ValueError("bad heartbeat")

    process = _sleeper(30)
    with pytest.raises(ValueError, match="bad heartbeat"):
        wait_process(process, name="job", heartbeat=buggy)
    assert process.wait(timeout=10) is not None


def _fast_stall(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(document_process, "STALL_IDLE_S", 0.6)
    monkeypatch.setattr(document_process, "_STALL_SAMPLE_S", 0.1)
    monkeypatch.setattr(document_process, "HEARTBEAT_FIRST_S", 60.0)


def test_a_job_doing_no_work_is_shown_stuck_and_a_stop_says_so(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A tree blocked on nothing (a sandboxed soffice on a denied pipe) is reported, not killed.

    The stall is reported at once, before the regular heartbeat is due, and stopping it
    names the stall. SABOTAGE: drop the work meter -> no stalled report -> red.
    """
    _fast_stall(monkeypatch)
    reports: list[float | None] = []
    stop = threading.Event()

    def heartbeat(_elapsed: float, stalled: float | None) -> None:
        reports.append(stalled)
        if stalled is not None:
            stop.set()  # the person sees the stall and stops the turn

    with pytest.raises(DocumentCancelled, match="no work for .* \\(stuck\\)"):
        wait_process(_sleeper(30), name="soffice", cancelled=stop.is_set, heartbeat=heartbeat)
    assert reports and reports[-1] is not None and reports[-1] >= 0.6
    assert "reason=document_process_stalled" in caplog.text


def test_a_busy_job_is_never_called_stuck(monkeypatch: pytest.MonkeyPatch) -> None:
    """A long conversion burning CPU is working, however long it runs."""
    _fast_stall(monkeypatch)
    monkeypatch.setattr(document_process, "HEARTBEAT_FIRST_S", 0.2)
    monkeypatch.setattr(document_process, "HEARTBEAT_EVERY_S", 0.2)
    reports: list[float | None] = []
    busy = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import time\nend = time.time() + 2.0\nwhile time.time() < end: pass\nprint('done')",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    stdout, _ = wait_process(busy, name="soffice", heartbeat=lambda _e, s: reports.append(s))
    assert stdout.strip() == "done"
    assert reports and all(stalled is None for stalled in reports)


def test_the_stall_message_names_the_macos_sandbox_cause(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(document_process.sys, "platform", "darwin")
    text = document_process.stall_message("soffice", 45)
    assert "no work for 45 s" in text and "sandbox denial" in text and "Stop the turn" in text
    monkeypatch.setattr(document_process.sys, "platform", "win32")
    assert "sandbox" not in document_process.stall_message("soffice", 45)


def _deny_listing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Behave like macOS Seatbelt: listing processes raises a plain PermissionError."""

    def denied(_self: psutil.Process, recursive: bool = False) -> list[psutil.Process]:
        raise PermissionError(1, "Operation not permitted (originated from sysctl() malloc 1/3)")

    monkeypatch.setattr(psutil.Process, "children", denied)


def test_a_fence_denying_the_process_list_never_fails_the_job(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The work meter is unknown, so the job runs on to its result instead of failing.

    SABOTAGE: catch only ``psutil.Error`` in ``_tree_cpu`` -> PermissionError at the
    first poll -> red.
    """
    _fast_stall(monkeypatch)
    _deny_listing(monkeypatch)
    caplog.set_level("INFO", logger=document_process.__name__)
    reports: list[float | None] = []
    stdout, _ = wait_process(
        _sleeper(1.0), name="soffice", heartbeat=lambda _e, s: reports.append(s)
    )
    assert stdout.strip() == "done"
    assert all(stalled is None for stalled in reports)
    assert "reason=document_process_unmetered" in caplog.text


def test_a_stop_ends_the_job_when_the_process_list_is_denied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """kill_tree survives the denied listing and still ends the job, typed as cancelled."""
    _deny_listing(monkeypatch)
    process = _sleeper(30)
    stop = threading.Event()
    threading.Timer(0.5, stop.set).start()
    with pytest.raises(DocumentCancelled):
        wait_process(process, name="soffice", cancelled=stop.is_set)
    assert process.poll() is not None


def test_an_unexpected_error_while_waiting_ends_the_job(monkeypatch: pytest.MonkeyPatch) -> None:
    """Whatever ends the wait early ends the process too: soffice is never orphaned."""

    def broken(_self: object, _now: float) -> float | None:
        raise RuntimeError("meter bug")

    monkeypatch.setattr(document_process._WorkMeter, "stalled_s", broken)
    process = _sleeper(30)
    with pytest.raises(RuntimeError, match="meter bug"):
        wait_process(process, name="soffice")
    assert process.poll() is not None


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups")
def test_a_confined_job_leads_its_group_and_ends_whole_without_a_listing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Inside a fence the job is its own group, so a stop ends its children unlisted."""
    monkeypatch.setenv("CLIO_DOCUMENT_CONFINED", "1")
    _deny_listing(monkeypatch)
    pid_file = Path.cwd() / f".clio-test-child-{time.monotonic_ns()}.pid"
    script = (
        "import pathlib, subprocess, sys, time;"
        "c = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']);"
        f"pathlib.Path({str(pid_file)!r}).write_text(str(c.pid)); time.sleep(60)"
    )
    stop = threading.Event()

    def stop_once_child_runs() -> None:
        deadline = time.monotonic() + 20
        while not pid_file.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        stop.set()

    threading.Thread(target=stop_once_child_runs, daemon=True).start()
    try:
        with watched(stop.is_set), pytest.raises(DocumentCancelled):
            run([sys.executable, "-c", script], cwd=Path.cwd())
        child_pid = int(pid_file.read_text())
    finally:
        pid_file.unlink(missing_ok=True)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            if psutil.Process(child_pid).status() == psutil.STATUS_ZOMBIE:
                break
        except psutil.NoSuchProcess:
            break
        time.sleep(0.1)
    else:
        pytest.fail("the job's child outlived the stop")
