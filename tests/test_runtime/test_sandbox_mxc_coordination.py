"""Separate processes share native qualification without duplicate probes or ACL fallback."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from typing import NoReturn

import pytest

from clio_agent.runtime import sandbox_cli, sandbox_codex
from clio_agent.runtime import sandbox_codex_mxc as mxc

_OWNER = """
import sys
from pathlib import Path
from types import SimpleNamespace
from clio_agent.runtime import sandbox_codex_mxc as mxc

root = Path(sys.argv[1])
verified = sys.argv[2] == 'True'
mxc.sys = SimpleNamespace(platform='win32', executable='python.exe', base_prefix='C:\\Python')
mxc._receipt_path = lambda: root / 'receipt.json'
mxc._identity = lambda binary, version='': {'binary': binary, 'version': version}

def probe(binary: str) -> bool:
    print('probing', flush=True)
    sys.stdin.readline()
    return verified

mxc.prepare_mxc('codex.exe', '0.167.0', probe=probe)
"""


@pytest.fixture
def receipt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Use a local receipt and a test-only executable identity on every CI platform."""
    path = tmp_path / "receipt.json"
    monkeypatch.setattr(
        mxc,
        "sys",
        SimpleNamespace(platform="win32", executable="python.exe", base_prefix="C:\\Python"),
    )
    monkeypatch.setattr(mxc, "_receipt_path", lambda: path)
    monkeypatch.setattr(
        mxc, "_identity", lambda binary, version="": {"binary": binary, "version": version}
    )
    return path


def _owner(root: Path, verified: bool) -> subprocess.Popen[str]:
    """Start a real process holding setup ownership until its stdin is released."""
    source = str(Path(mxc.__file__).resolve().parents[2])
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join(filter(None, [source, os.getenv("PYTHONPATH")])),
    }
    return subprocess.Popen(
        [sys.executable, "-c", _OWNER, str(root), str(verified)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )


def _stop(process: subprocess.Popen[str]) -> None:
    """Always retire the test-owned child, including after a failed assertion."""
    if process.poll() is None:
        process.kill()
    process.communicate(timeout=10)


@pytest.mark.parametrize("verified", [True, False])
def test_processes_share_the_completed_verification(receipt: Path, verified: bool) -> None:
    process = _owner(receipt.parent, verified)
    waiting = threading.Event()

    def progress(message: str) -> None:
        if "Another CLIO process" in message:
            waiting.set()

    def duplicate_probe(binary: str) -> bool:
        pytest.fail("The waiting process must reuse the owner's completed proof")

    try:
        with ThreadPoolExecutor(max_workers=2) as workers:
            assert process.stdout is not None
            started = workers.submit(process.stdout.readline)
            try:
                assert started.result(timeout=10) == "probing\n"
                follower = workers.submit(
                    mxc.prepare_mxc,
                    "codex.exe",
                    "0.167.0",
                    probe=duplicate_probe,
                    progress=progress,
                )
                assert waiting.wait(timeout=10)
                _, stderr = process.communicate(input="finish\n", timeout=10)
                assert process.returncode == 0, stderr
                result = follower.result(timeout=10)
                assert result["status"] == ("verified" if verified else "unavailable")
                assert mxc.mxc_ready("codex.exe", version="0.167.0") is verified
            finally:
                # Unblock both pipe and lock waiters before the executor joins them.
                _stop(process)
    finally:
        _stop(process)


def test_busy_owner_does_not_publish_failure_or_start_another_probe(
    receipt: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    previous = {"unrelated_previous_receipt": True}
    receipt.write_text(json.dumps(previous), encoding="utf-8")
    process = _owner(receipt.parent, True)
    monkeypatch.setattr(mxc, "_SETUP_LOCK_TIMEOUT_S", 0)

    def duplicate_probe(binary: str) -> bool:
        pytest.fail("A busy owner must not start another qualification")

    try:
        with ThreadPoolExecutor(max_workers=1) as workers:
            assert process.stdout is not None
            started = workers.submit(process.stdout.readline)
            try:
                assert started.result(timeout=10) == "probing\n"
                assert mxc.prepare_mxc("codex.exe", "0.167.0", probe=duplicate_probe) == {
                    "status": "busy",
                    "reason": "codex_mxc_setup_in_progress",
                }
                assert json.loads(receipt.read_text(encoding="utf-8")) == previous
                assert not mxc.mxc_ready("codex.exe", version="0.167.0")
            finally:
                _stop(process)
    finally:
        _stop(process)


def test_failed_owner_releases_setup_for_the_next_process(receipt: Path) -> None:
    process = _owner(receipt.parent, True)
    try:
        with ThreadPoolExecutor(max_workers=1) as workers:
            assert process.stdout is not None
            started = workers.submit(process.stdout.readline)
            try:
                assert started.result(timeout=10) == "probing\n"
            finally:
                _stop(process)
        assert not receipt.exists()
        assert (
            mxc.prepare_mxc("codex.exe", "0.167.0", probe=lambda binary: True)["status"]
            == "verified"
        )
    finally:
        _stop(process)


def test_busy_native_setup_cannot_start_legacy_repair() -> None:
    def forbidden(*args: object, **kwargs: object) -> NoReturn:
        pytest.fail("A busy native verifier must not start helpers, UAC or ACL repair")

    result = sandbox_cli.provision_codex_windows(
        platform="win32",
        detection=sandbox_codex.CodexDetection(
            True,
            "codex.exe",
            "0.167.0",
            sandbox_codex.REASON_CODEX_DETECTED,
            source=sandbox_codex.CODEX_SOURCE_BUNDLED,
        ),
        mxc_preparer=lambda *args, **kwargs: {
            "status": "busy",
            "reason": "codex_mxc_setup_in_progress",
        },
        elevator=forbidden,
        gate=forbidden,
        grantor=forbidden,
        helper_preparer=forbidden,
    )
    assert not result.ok
    assert not result.elevated
    assert result.reason == "codex_mxc_setup_in_progress"
