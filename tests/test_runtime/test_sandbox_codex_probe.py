"""A qualification probe keeps Codex's words and never leaves a timed-out tree running."""

from __future__ import annotations

import sys
from pathlib import Path

import psutil
import pytest

from clio_agent.runtime import sandbox_cli, sandbox_codex
from clio_agent.runtime import sandbox_codex_probe as probe


def test_failed_launch_keeps_codex_stderr() -> None:
    script = "import sys; sys.stderr.write('MXC launcher: rejects\\n  the config\\n'); sys.exit(3)"
    run = probe.run_probe([sys.executable, "-I", "-c", script], timeout=30)
    assert (run.returncode, run.timed_out) == (3, False)
    assert run.stderr == "MXC launcher: rejects the config"
    assert run.describe() == "codex exited 3: MXC launcher: rejects the config"


def test_stderr_is_bounded_to_its_tail() -> None:
    script = "import sys; sys.stderr.write('x' * 5000 + 'END')"
    run = probe.run_probe([sys.executable, "-I", "-c", script], timeout=30)
    assert run.stderr.endswith("END")
    assert len(run.stderr) == probe.STDERR_TAIL_CHARS + 3


def test_probe_names_the_codex_home_clio_wrote_to(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    script = "import os, sys; sys.stderr.write(os.environ.get('CODEX_HOME', '')); sys.exit(1)"
    run = probe.run_probe([sys.executable, "-I", "-c", script], timeout=30)
    assert run.stderr == str(tmp_path / ".codex")
    assert probe.codex_home_dir() == tmp_path / ".codex"


def test_caller_codex_home_is_respected(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "mine"))
    assert probe.codex_home_env() == {}
    assert probe.codex_home_dir() == tmp_path / "mine"


@pytest.mark.timeout(60)  # A deliberate 2-second probe deadline plus tree teardown.
def test_timeout_kills_the_whole_tree_and_reports_the_timeout(tmp_path: Path) -> None:
    pid_file = tmp_path / "grandchild.pid"
    grandchild = "import time; time.sleep(120)"
    script = (
        "import subprocess, sys, time\n"
        f"child = subprocess.Popen([sys.executable, '-I', '-c', {grandchild!r}])\n"
        f"open({str(pid_file)!r}, 'w').write(str(child.pid))\n"
        "sys.stderr.write('still resolving a share'); sys.stderr.flush()\n"
        "time.sleep(120)\n"
    )
    run = probe.run_probe([sys.executable, "-I", "-c", script], timeout=2)
    assert (run.returncode, run.timed_out) == (None, True)
    assert run.describe() == "codex timed out: still resolving a share"
    pid = int(pid_file.read_text())
    assert not psutil.pid_exists(pid) or psutil.Process(pid).status() == psutil.STATUS_ZOMBIE


def test_verdict_unpacks_like_the_pair_and_carries_detail() -> None:
    verdict = probe.EnforcementVerdict(False, "codex_enforcement_unverified", "setup refresh")
    enforced, reason = verdict
    assert (enforced, reason, verdict.detail) == (
        False,
        "codex_enforcement_unverified",
        "setup refresh",
    )


def test_unverified_fence_message_names_both_causes(tmp_path: Path) -> None:
    """Each backend's own reason reaches the person; neither is reported as a write escape."""
    result = sandbox_cli.provision_codex_windows(
        platform="win32",
        allow_elevation=False,
        detection=sandbox_codex.CodexDetection(
            True, "codex.exe", "0.167.0", sandbox_codex.REASON_CODEX_DETECTED, "system"
        ),
        mxc_preparer=lambda *args, **kwargs: {
            "status": "unavailable",
            "reason": "codex_mxc_launch_failed",
            "detail": "codex exited 1: MXC launcher: Filesystem path could not be resolved",
        },
        gate=lambda **kwargs: (False, sandbox_codex.REASON_CODEX_ENFORCEMENT_UNVERIFIED),
        grantor=lambda: [],
        verifier=lambda *args, **kwargs: probe.EnforcementVerdict(
            False,
            sandbox_codex.REASON_CODEX_ENFORCEMENT_UNVERIFIED,
            "Codex did not start the test command (codex exited 1: setup refresh had errors)",
        ),
        marker_writer=lambda *args, **kwargs: None,
    )
    assert not result.ok
    assert "Native MXC (codex_mxc_launch_failed)" in result.detail
    assert "could not be resolved" in result.detail
    assert "setup refresh had errors" in result.detail
    assert "Set up protected execution" in result.next_action


def test_a_plain_pair_from_an_injected_verifier_still_reads(tmp_path: Path) -> None:
    result = sandbox_cli.provision_codex_windows(
        platform="win32",
        allow_elevation=False,
        detection=sandbox_codex.CodexDetection(
            True, "codex.exe", "0.167.0", sandbox_codex.REASON_CODEX_DETECTED, "system"
        ),
        mxc_preparer=lambda *args, **kwargs: {"status": "unavailable", "reason": "x"},
        gate=lambda **kwargs: (False, sandbox_codex.REASON_CODEX_ENFORCEMENT_UNVERIFIED),
        grantor=lambda: [],
        verifier=lambda *args, **kwargs: (False, sandbox_codex.REASON_CODEX_ENFORCEMENT_UNVERIFIED),
        marker_writer=lambda *args, **kwargs: None,
    )
    assert "the confined write was not refused" in result.detail
