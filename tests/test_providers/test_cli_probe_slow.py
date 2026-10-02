"""CLI probes tell a slow CLI from a missing or signed-out one (#1577 3.10).

``claude --version`` / ``codex --version`` (5 s) and ``claude auth status`` (20 s) used a
flat ``subprocess`` timeout: a CLI cold-starting on a slow machine read as "version
unreadable", "unsupported" or "not signed in". Past the usual bound the probe now waits
while the CLI keeps working, and one that stops working is reported typed as slow /
unresponsive. Each test runs a REAL child process standing in for the CLI: one that burns
CPU before answering (slow but working) and one that sleeps (unresponsive).
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

from clio_agent.providers.components import client_binary as cb
from clio_agent.providers.model_discovery import claude_code as cc_discovery
from clio_agent.runtime import progress, sandbox_codex
from clio_agent.runtime.progress import ProbeUnresponsiveError

_CLI = """
import json, sys, time
mode, delay = {mode!r}, {delay!r}
if mode == "busy":
    end = time.monotonic() + delay
    while time.monotonic() < end:
        pass
else:
    time.sleep(delay)
args = sys.argv[1:]
if args == ["--version"]:
    print("codex-cli 0.150.0")
elif args == ["auth", "status"]:
    print(json.dumps({{"loggedIn": True}}))
"""


def _fake_cli(tmp_path: Path, mode: str, delay: float) -> str:
    """A runnable CLI (a ``.cmd`` shim on Windows) backed by a Python script."""
    script = tmp_path / f"cli_{mode}.py"
    script.write_text(_CLI.format(mode=mode, delay=delay), encoding="utf-8")
    if os.name == "nt":
        shim = tmp_path / f"cli_{mode}.cmd"
        shim.write_text(f'@"{sys.executable}" "{script}" %*\r\n', encoding="utf-8")
    else:
        shim = tmp_path / f"cli_{mode}"
        shim.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n', encoding="utf-8")
        shim.chmod(0o755)
    return str(shim)


@pytest.fixture(autouse=True)
def _short_bounds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cb, "VERSION_PROBE_TIMEOUT_S", 1.0)
    monkeypatch.setattr(sandbox_codex, "_VERSION_PROBE_TIMEOUT_S", 1.0)


def test_a_slow_but_working_cli_reports_its_version(tmp_path: Path) -> None:
    """SABOTAGE: a flat ``subprocess.run(timeout=...)`` -> "" -> red."""
    assert cb.probe_version(_fake_cli(tmp_path, "busy", 3.0)) == "0.150.0"


def test_an_unresponsive_cli_is_typed_not_unreadable(tmp_path: Path) -> None:
    """SABOTAGE: return "" on a timeout (the old swallow) -> no ProbeUnresponsiveError -> red."""
    started = time.monotonic()
    with pytest.raises(ProbeUnresponsiveError) as caught:
        cb.probe_version(_fake_cli(tmp_path, "idle", 30.0))
    assert caught.value.reason == progress.REASON_NO_PROGRESS
    assert time.monotonic() - started < 15


def test_codex_detection_reports_an_unresponsive_probe_as_such(tmp_path: Path) -> None:
    """SABOTAGE: let the probe failure fold to "" -> codex_version_unsupported -> red."""
    binary = _fake_cli(tmp_path, "idle", 30.0)
    det = sandbox_codex.detect_codex(
        which=lambda name: binary, bundled_root=lambda: None, platform="linux"
    )
    assert det.installed is True
    assert det.reason == sandbox_codex.REASON_CODEX_VERSION_PROBE_UNRESPONSIVE


def test_a_slow_sign_in_check_still_finds_the_sign_in(tmp_path: Path) -> None:
    """SABOTAGE: flat timeout on ``auth status`` -> "timed out" -> not signed in -> red."""
    signed_in, reason = cc_discovery._auth_status(_fake_cli(tmp_path, "busy", 3.0), timeout=1.0)
    assert (signed_in, reason) == (True, "")


def test_an_unresponsive_sign_in_check_is_not_a_sign_out(tmp_path: Path) -> None:
    """SABOTAGE: report the timeout as "not signed in" -> red."""
    signed_in, reason = cc_discovery._auth_status(_fake_cli(tmp_path, "idle", 30.0), timeout=1.0)
    assert signed_in is False
    assert reason.startswith(cc_discovery.AUTH_CHECK_UNRESPONSIVE)
    assert "not a sign-out" in reason
