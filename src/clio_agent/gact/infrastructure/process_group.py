"""Reap what a supervised server left in its worker's process group (F014).

Standard library only and POSIX-only (process groups): the native-service
supervisor (:mod:`~clio_agent.gact.infrastructure.node_service`) runs on the
target, so this module is shipped beside it as :data:`SHIPPED_NAME` the way
the reuse helper is. The caller passes its exact process ``identity`` check,
so a zombie or a reused PID is never counted as a member.
"""

from __future__ import annotations

import os
import signal
import subprocess
import time
from collections.abc import Callable
from pathlib import Path

#: The file name this module is written under in a service directory.
SHIPPED_NAME = "clio_process_group.py"

Identity = Callable[[int], str]


def source() -> str:
    """This file's text, shipped to targets as :data:`SHIPPED_NAME`."""
    return Path(__file__).read_text(encoding="utf-8")


def group_members(group: int, identity: Identity) -> list[int]:
    """Live processes of a POSIX process group other than the caller."""
    if Path("/proc/self/stat").exists():
        pids = [int(entry.name) for entry in Path("/proc").iterdir() if entry.name.isdigit()]
    else:
        pids = _ps_pids()
    members = []
    for pid in pids:
        if pid == os.getpid():
            continue
        try:
            if os.getpgid(pid) == group and identity(pid):
                members.append(pid)
        except OSError:
            continue
    return members


def _ps_pids() -> list[int]:
    """Every PID ``ps`` lists (a host without /proc)."""
    try:
        done = subprocess.run(
            ["ps", "-A", "-o", "pid="], capture_output=True, text=True, timeout=10, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return []
    return [int(word) for word in done.stdout.split() if word.isdigit()]


def release_group(identity: Identity, timeout: float = 10) -> None:
    """End descendants left in the worker's own group after its child exited.

    A server's engine processes inherit the output pipe; if the API server dies they would keep the
    GPU and the pipe while nothing supervises them.
    """
    group = os.getpgrp()
    for sig in (signal.SIGTERM, signal.SIGKILL):
        for pid in group_members(group, identity):
            try:
                os.kill(pid, sig)
            except OSError:
                continue
        deadline = time.monotonic() + timeout
        while group_members(group, identity) and time.monotonic() < deadline:
            time.sleep(0.1)
        if not group_members(group, identity):
            return
