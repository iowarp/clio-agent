"""Terminating a task's tree also stops descendants that started a session of their own.

A fenced kernel runs under ``bwrap --new-session``: its sandbox leaves the task's
process group, so a group kill alone left a cancelled script running.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time

import psutil
import pytest

from clio_agent.runtime.task_process_tree import TaskProcessTree, task_spawn_options

pytestmark = pytest.mark.skipif(
    sys.platform == "win32" or not os.path.isdir("/proc"), reason="process groups via /proc"
)

_ESCAPE = (
    "import subprocess, sys\n"
    "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'],"
    " start_new_session=True)\n"
    "print(child.pid, flush=True)\n"
    "child.wait()\n"
)


def _ended(process: psutil.Process) -> bool:
    try:
        return process.status() == psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return True


def test_terminate_stops_a_descendant_in_another_session() -> None:
    """SABOTAGE: drop the descendant kill from ``terminate`` and the escaped sleeper
    outlives its tree."""
    owner = subprocess.Popen(
        [sys.executable, "-c", _ESCAPE], stdout=subprocess.PIPE, **task_spawn_options({})
    )
    assert owner.stdout is not None
    escaped = psutil.Process(int(owner.stdout.readline()))
    try:
        assert os.getpgid(escaped.pid) != owner.pid  # out of the group's reach
        TaskProcessTree(owner.pid).terminate()
        owner.wait(timeout=10)
        deadline = time.monotonic() + 10
        while not _ended(escaped) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert _ended(escaped)
    finally:
        if not _ended(escaped):
            escaped.kill()
        owner.stdout.close()
