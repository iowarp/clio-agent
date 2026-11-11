"""An exited, unreaped descendant does not keep a task's process group running."""

from __future__ import annotations

import os
import subprocess
import sys
import time

import pytest

from clio_agent.runtime.task_process_tree import TaskProcessTree, task_spawn_options

pytestmark = pytest.mark.skipif(
    sys.platform == "win32" or not os.path.isdir("/proc"), reason="process groups via /proc"
)


def _zombie_of_ours() -> subprocess.Popen[bytes]:
    """A child in its own group that has exited and that only we may reap."""
    child = subprocess.Popen(["true"], **task_spawn_options({}))
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        with open(f"/proc/{child.pid}/stat", encoding="ascii") as handle:
            if handle.read().rsplit(")", 1)[1].split()[0] == "Z":
                return child
        time.sleep(0.01)
    raise AssertionError("the child never exited")


def test_an_unreaped_member_does_not_keep_the_group_running() -> None:
    """SABOTAGE: return ``True`` from ``_group_running`` (the old check) and the tree
    stays active until the zombie is reaped."""
    child = _zombie_of_ours()
    tree = TaskProcessTree(child.pid)
    try:
        os.killpg(child.pid, 0)  # the group still exists: a zombie holds it
        assert not tree.active()
    finally:
        child.wait()
        tree.close()


def test_a_running_descendant_still_keeps_it_running() -> None:
    shell = subprocess.Popen(
        ["bash", "-c", "(sleep 1 &); exit 0"], stdout=subprocess.DEVNULL, **task_spawn_options({})
    )
    tree = TaskProcessTree(shell.pid)
    try:
        shell.wait()
        assert tree.active()  # the background sleep runs in the shell's group
        deadline = time.monotonic() + 10
        while tree.active() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert not tree.active()
    finally:
        tree.close()
