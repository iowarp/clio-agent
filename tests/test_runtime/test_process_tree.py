"""Process-tree reaping + census (#900).

CLIO's child tree (MCP stdio children + pooled SDK CLI) must die with the server even
on a HARD kill. On Windows that guarantee is a ``KILL_ON_JOB_CLOSE`` Job Object; the
shared clio-core daemon must break OUT of the job and survive. These tests pin:

* the Job Object limit flags (kill-on-close + breakaway),
* install semantics per platform,
* the daemon spawn breaking away from the job (the exclusion the issue calls out),
* the clean-shutdown SDK-pool teardown emitting its typed reason,
* the doctor census + reaper rows,
* and — on Windows — a real spawn + hard-kill + assert-no-orphans.
"""

from __future__ import annotations

import logging
import subprocess
import sys
import time

import pytest

from clio_agent.runtime import process_tree as pt
from clio_agent.runtime.status import IntegrationState


def test_windows_job_limit_flags_include_kill_on_close_and_breakaway() -> None:
    """The Job Object must reap the tree on close AND permit the daemon's breakaway."""
    assert pt.WINDOWS_JOB_LIMIT_FLAGS & pt._JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    assert pt.WINDOWS_JOB_LIMIT_FLAGS & pt._JOB_OBJECT_LIMIT_BREAKAWAY_OK
    # Exactly those two bits — nothing that would, e.g., silently break every child away.
    assert pt.WINDOWS_JOB_LIMIT_FLAGS == 0x2000 | 0x0800


@pytest.mark.skipif(not sys.platform.startswith("win"), reason="Job Object is Windows-only")
def test_install_child_reaper_active_on_win32() -> None:
    """On Windows the reaper installs a live Job Object and caches the result."""
    result = pt.install_child_reaper()
    assert result.mechanism == pt.MECHANISM_WINDOWS_JOB
    assert result.active is True
    assert result.reason in {"job_assigned", "job_already_assigned"}
    assert pt.child_reaper_status() is result
    # Idempotent: a second call does not create a second job.
    again = pt.install_child_reaper()
    assert again.active is True


@pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX-only delegation path")
def test_install_child_reaper_delegated_on_posix() -> None:
    """On POSIX there is no process-wide job; the reaper reports honest delegation."""
    result = pt.install_child_reaper()
    assert result.mechanism == pt.MECHANISM_POSIX_DELEGATED
    assert result.active is False
    assert result.reason == "delegated_to_pdeathsig"
    assert "pdeathsig" in result.details["note"]


def test_daemon_spawn_breaks_away_from_job_on_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    """The shared clio-core daemon must be spawned OUTSIDE the server's job (#900).

    Pin the Windows branch of ``arc.storage._detached_popen_kwargs`` — it must carry
    ``CREATE_BREAKAWAY_FROM_JOB`` so a server hard-kill never takes the shared daemon
    with it. Portable: forces the win32 branch so the assertion runs on any CI OS.
    """
    from clio_agent.arc import storage

    monkeypatch.setattr("sys.platform", "win32")
    kwargs = storage._detached_popen_kwargs()
    breakaway = getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0x01000000)
    assert kwargs["creationflags"] & breakaway, (
        "clio-core daemon spawn lost CREATE_BREAKAWAY_FROM_JOB — a server hard-kill "
        "would reap the SHARED daemon (regression of #900)"
    )


def test_teardown_pooled_sdk_transports_closes_both_pools_and_logs(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Clean shutdown closes every SDK client and emits the typed reason."""
    from clio_agent.providers import claude_code_sdk_pool, claude_code_sessions, codex_stream

    calls: list[str] = []
    monkeypatch.setattr(
        claude_code_sessions._STREAM_CLIENT_POOL,
        "close_blocking",
        lambda: calls.append("stream"),
    )
    monkeypatch.setattr(
        claude_code_sdk_pool._SDK_SESSION_POOL,
        "close",
        lambda: calls.append("sdk"),
    )
    monkeypatch.setattr(
        codex_stream._SDK_CLIENT,
        "close_blocking",
        lambda: calls.append("codex"),
    )

    with caplog.at_level(logging.INFO, logger="clio_agent.runtime.process_tree"):
        outcome = pt.teardown_pooled_sdk_transports()

    assert calls == ["stream", "sdk", "codex"]
    assert outcome == {
        "stream_client_pool": "closed",
        "sdk_session_pool": "closed",
        "codex_sdk_client": "closed",
    }
    assert any("reason=sdk_pools_closed" in rec.message for rec in caplog.records)


def test_teardown_records_per_pool_failure_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    """A pool that fails to close is recorded with a structured reason, never swallowed."""
    from clio_agent.providers import claude_code_sdk_pool, claude_code_sessions

    def _boom() -> None:
        raise RuntimeError("connect wedged")

    monkeypatch.setattr(claude_code_sessions._STREAM_CLIENT_POOL, "close_blocking", _boom)
    monkeypatch.setattr(claude_code_sdk_pool._SDK_SESSION_POOL, "close", lambda: None)

    outcome = pt.teardown_pooled_sdk_transports()
    assert outcome["stream_client_pool"].startswith("error:")
    assert outcome["sdk_session_pool"] == "closed"


def test_probe_census_lists_children_with_pid_name_age_kind() -> None:
    """The census row names each live child (pid/name/age/kind) so leakage is visible."""
    children = [
        {"pid": 111, "name": "clio-kit.exe", "age_seconds": 12.0, "kind": "mcp_stdio"},
        {"pid": 222, "name": "claude", "age_seconds": 3.0, "kind": "sdk_cli"},
    ]
    rows = pt.probe_process_tree(reaper=None, _reaper_unset=False, children=children)
    assert len(rows) == 1  # reaper explicitly None → only the census row
    census = rows[0]
    assert census.name == "child_processes"
    assert census.state is IntegrationState.READY
    assert census.details["count"] == 2
    assert census.details["children"] == children
    assert "mcp_stdio=1" in census.summary
    assert "sdk_cli=1" in census.summary


def test_probe_census_empty_is_ready_no_children() -> None:
    """Zero children is a clean READY row (the healthy standalone-doctor case)."""
    rows = pt.probe_process_tree(reaper=None, _reaper_unset=False, children=[])
    assert len(rows) == 1
    assert rows[0].details["reason"] == "no_children"
    assert rows[0].details["count"] == 0


def test_probe_reaper_row_active_windows() -> None:
    """An active Job Object surfaces a READY reaper row advertising hard-kill reaping."""
    reaper = pt.ChildReaperResult(
        mechanism=pt.MECHANISM_WINDOWS_JOB, active=True, reason="job_assigned"
    )
    rows = pt.probe_process_tree(reaper=reaper, children=[])
    reaper_row = next(r for r in rows if r.name == "child_reaper")
    assert reaper_row.state is IntegrationState.READY
    assert "hard-kill-reap" in reaper_row.capabilities


def test_probe_reaper_row_degraded_when_install_failed() -> None:
    """A failed Windows install surfaces a DEGRADED row (the hard-kill loss is visible)."""
    reaper = pt.ChildReaperResult(
        mechanism=pt.MECHANISM_UNAVAILABLE, active=False, reason="job_assign_failed"
    )
    rows = pt.probe_process_tree(reaper=reaper, children=[])
    reaper_row = next(r for r in rows if r.name == "child_reaper")
    assert reaper_row.state is IntegrationState.DEGRADED
    assert reaper_row.fallback == "graceful-terminate-tree-only"
    assert "job_assign_failed" in reaper_row.summary


def test_classify_child_kinds() -> None:
    """Coarse classification names WHAT leaked, not just a count."""
    assert pt._classify_child("clio-kit.exe") == "mcp_stdio"
    assert pt._classify_child("claude") == "sdk_cli"
    assert pt._classify_child("clio_run.exe") == "clio_core_daemon"
    assert pt._classify_child("random-thing") == "other"


# --------------------------------------------------------------------------- #
# The leak-detector: a real spawn + HARD kill + assert-no-orphans on Windows.  #
# --------------------------------------------------------------------------- #

_CHILD_SRC = """
import subprocess, sys, time
from clio_agent.runtime.process_tree import install_child_reaper

result = install_child_reaper()
assert result.active, result
# A grandchild spawned WITHOUT breakaway inherits the job; it must die when this
# process (the only job-handle holder) is hard-killed.
gc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
open(sys.argv[1], "w").write(str(gc.pid))
time.sleep(120)
"""


@pytest.mark.skipif(not sys.platform.startswith("win"), reason="hard-kill reap is Windows-only")
def test_hard_kill_reaps_grandchild_via_job_object(tmp_path) -> None:
    """HARD-kill a job-owning process; its in-job grandchild must be reaped by the OS.

    This is the acceptance behaviour of #900: no graceful teardown runs (``proc.kill()``
    is a bare ``TerminateProcess``), yet the grandchild dies because the Job Object's
    KILL_ON_JOB_CLOSE fires the instant the last job handle (held by the killed parent)
    closes. Without the job an orphaned ``python.exe`` would idle for 120s.
    """
    import psutil

    pidfile = tmp_path / "gc.pid"
    parent = subprocess.Popen([sys.executable, "-c", _CHILD_SRC, str(pidfile)])
    try:
        deadline = time.time() + 30
        while not pidfile.exists() and time.time() < deadline:
            if parent.poll() is not None:
                raise AssertionError("job-owning child exited before spawning a grandchild")
            time.sleep(0.1)
        assert pidfile.exists(), "child never recorded the grandchild pid"
        gc_pid = int(pidfile.read_text())
        assert psutil.pid_exists(gc_pid), "grandchild should be alive while the parent lives"

        parent.kill()  # bare TerminateProcess: NO graceful cleanup runs
        parent.wait(timeout=10)

        for _ in range(100):  # KILL_ON_JOB_CLOSE should reap the grandchild promptly
            if not psutil.pid_exists(gc_pid):
                break
            time.sleep(0.1)
        assert not psutil.pid_exists(gc_pid), (
            "grandchild orphaned after a hard parent kill — Job Object reaping failed (#900)"
        )
    finally:
        if parent.poll() is None:
            parent.kill()
        if pidfile.exists():
            try:
                psutil.Process(int(pidfile.read_text())).kill()
            except (psutil.NoSuchProcess, ValueError):
                pass
