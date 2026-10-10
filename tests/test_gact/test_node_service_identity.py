"""Process identity in the native-service supervisor, with and without /proc.

Linux uses /proc (boot id + start tick); a POSIX host without /proc (macOS,
BSD) uses ``ps``'s start time (DIRECTIVES 11). Both branches run here against
real processes.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest

from clio_agent.gact.infrastructure import node_service, process_group

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX supervisor")


@pytest.fixture(params=["proc", "ps"])
def branch(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> str:
    if request.param == "proc" and not node_service._has_proc():
        pytest.skip("no /proc on this host")
    if request.param == "ps":
        monkeypatch.setattr(node_service, "_has_proc", lambda: False)
    return str(request.param)


def test_a_live_process_has_a_stable_identity(branch: str) -> None:
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        first = node_service.identity(child.pid)
        assert first
        assert node_service.identity(child.pid) == first
        assert first.startswith("ps:") == (branch == "ps")
        receipt = {"pid": child.pid, "process_identity": first}
        assert node_service.alive(receipt)
    finally:
        child.kill()
        child.wait()
    assert node_service.identity(child.pid) == ""
    assert not node_service.alive(receipt)


def test_a_zombie_is_not_alive(branch: str) -> None:
    del branch
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    try:
        for _ in range(100):
            if node_service.identity(child.pid) == "":
                break
            time.sleep(0.05)
        assert node_service.identity(child.pid) == ""  # exited, not yet reaped
    finally:
        child.wait()


def test_group_members_lists_the_live_group(branch: str) -> None:
    del branch
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True
    )
    try:
        assert process_group.group_members(child.pid, node_service.identity) == [child.pid]
    finally:
        child.kill()
        child.wait()
    assert process_group.group_members(child.pid, node_service.identity) == []


def test_no_pid_has_no_identity() -> None:
    assert node_service._ps_identity(0) == ""


def test_the_shipped_process_group_helper_runs_without_clio(tmp_path: Path) -> None:
    # Targets run it beside the supervisor as clio_process_group.py, stdlib only (-S: no site).
    (tmp_path / process_group.SHIPPED_NAME).write_text(process_group.source(), encoding="utf-8")
    probe = (
        "import os, sys; sys.path.insert(0, sys.argv[1]); import clio_process_group as g; "
        "print(g.group_members(os.getpgrp(), lambda pid: 'x'))"
    )
    done = subprocess.run(
        [sys.executable, "-I", "-S", "-c", probe, str(tmp_path)],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip().startswith("[")
