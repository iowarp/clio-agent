"""Model-download worker identity on the real host (Linux /proc, macOS libproc).

Without a creation-time identity a live worker is never recognized, so a
repeated request would spawn a duplicate download. These run against real
processes on both POSIX hosts.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from clio_agent.gact.infrastructure import node_models

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX identity paths")
REAL_POPEN = subprocess.Popen


def _sleeper() -> subprocess.Popen[bytes]:
    return REAL_POPEN([sys.executable, "-c", "import time; time.sleep(30)"])


def test_a_live_worker_has_a_stable_identity() -> None:
    child = _sleeper()
    try:
        first = node_models.process_identity(child.pid)
        assert first
        assert node_models.process_identity(child.pid) == first
        assert first.startswith("darwin:") == (sys.platform == "darwin")
        job = {"pid": child.pid, "process_identity": first}
        assert node_models.alive(job)
    finally:
        child.kill()
        child.wait()
    assert node_models.process_identity(child.pid) == ""
    assert not node_models.alive(job)


def test_an_exited_unreaped_worker_is_not_alive() -> None:
    child = REAL_POPEN([sys.executable, "-c", "pass"])
    try:
        for _ in range(100):
            if node_models.process_identity(child.pid) == "":
                break
            time.sleep(0.05)
        assert node_models.process_identity(child.pid) == ""
    finally:
        child.wait()


def test_a_second_request_reuses_the_live_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real spawned worker is recognized, so a repeated request does not launch another."""
    workers: list[subprocess.Popen[bytes]] = []

    def spawn(argv: list[str], **kwargs: Any) -> subprocess.Popen[bytes]:
        del argv, kwargs
        workers.append(_sleeper())
        return workers[-1]

    monkeypatch.setattr(node_models.subprocess, "Popen", spawn)
    root = tmp_path / "operations"
    root.mkdir()
    request = {"repository": "org/model", "revision": "main", "destination": str(tmp_path / "m")}
    try:
        first = node_models.start(root, request, "# inert")
        second = node_models.start(root, request, "# inert")
        assert first["id"] == second["id"]
        assert len(workers) == 1
    finally:
        for worker in workers:
            worker.kill()
            worker.wait()


def test_no_pid_has_no_identity() -> None:
    assert node_models.process_identity(0) == ""
