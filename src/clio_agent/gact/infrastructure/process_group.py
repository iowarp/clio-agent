"""Reap what a supervised server left in its worker's process group (F014).

Standard library only and POSIX-only (process groups): the native-service
supervisor (:mod:`~clio_agent.gact.infrastructure.node_service`) runs on the
target, so this module is shipped beside it as :data:`SHIPPED_NAME` the way
the reuse helper is. The caller passes its exact process ``identity`` check,
so a zombie or a reused PID is never counted as a member.
"""

from __future__ import annotations

import os
import select
import signal
import subprocess
import sys
import threading
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


class OwnerWatch:
    """A worker's watch on the CLIO server it serves (see :func:`watch_owner`)."""

    def __init__(self) -> None:
        self.fired = threading.Event()
        self.thread: threading.Thread | None = None

    def settle(self) -> bool:
        """Whether the owner died; if so, wait until the watch has ended this worker's group."""
        if self.fired.is_set() and self.thread is not None:
            self.thread.join()
        return self.fired.is_set()


def watch_owner(
    owner: str, identity: Identity, publish_stop: Callable[[], None]
) -> OwnerWatch | None:
    """End this worker's process group once its owning server (``<pid>:<identity>``) is gone.

    The POSIX counterpart of the Windows Job Object a forced server kill ends: without it,
    a detached worker keeps the service it started (SearXNG) running after CLIO is gone.
    ``publish_stop`` records the stop before the group goes. Only a group leader watches.
    """
    if os.getpgrp() != os.getpid():
        return None
    watch = OwnerWatch()
    watch.thread = threading.Thread(
        target=_end_with_owner, args=(owner, identity, publish_stop, watch), daemon=True
    )
    watch.thread.start()
    return watch


def _end_with_owner(
    owner: str, identity: Identity, publish_stop: Callable[[], None], watch: OwnerWatch
) -> None:
    """SIGTERM the whole group, this worker included: its stop handler then keeps the
    published receipt, and :func:`release_group` escalates for members that stay."""
    pid, _, expected = owner.partition(":")
    await_exit(int(pid), expected, identity)
    watch.fired.set()
    publish_stop()
    try:
        os.killpg(os.getpgrp(), signal.SIGTERM)
    except OSError:
        pass
    release_group(identity)


def await_exit(pid: int, expected: str, identity: Identity) -> None:
    """Block until the exact process ``pid`` (identity ``expected``) has exited.

    Registers for its exit first (kqueue on macOS/BSD, a pidfd on Linux), then checks the
    identity, so a PID reused in between reads as the owner already gone. A host with
    neither primitive polls the identity once a second.
    """
    try:
        # The platform test narrows ``select`` for the type-checker: no kqueue on Linux/Windows.
        if sys.platform != "linux" and sys.platform != "win32" and hasattr(select, "kqueue"):
            queue = select.kqueue()
            try:
                exit_event = select.kevent(
                    pid,
                    filter=select.KQ_FILTER_PROC,
                    flags=select.KQ_EV_ADD | select.KQ_EV_ONESHOT,
                    fflags=select.KQ_NOTE_EXIT,
                )
                queue.control([exit_event], 0)
                if identity(pid) == expected:
                    queue.control(None, 1)
                return
            finally:
                queue.close()
        if hasattr(os, "pidfd_open"):
            descriptor = os.pidfd_open(pid)
            try:
                if identity(pid) == expected:
                    poller = select.poll()
                    poller.register(descriptor, select.POLLIN)
                    poller.poll()
                return
            finally:
                os.close(descriptor)
    except ProcessLookupError:
        return
    except OSError:
        pass  # No exit notification on this kernel: poll instead.
    while identity(pid) == expected:
        time.sleep(1)
