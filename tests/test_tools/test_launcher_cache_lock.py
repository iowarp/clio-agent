"""Liveness-driven lock around the shared uvx/uv-run launcher cache (#1237 hotfix).

Owner ruling (2026-08-20): never race a deadline against a LIVE holder — wait
however long the holder's legitimate work takes. A DEAD holder's lock is an
abandoned artifact: break it, typed and loud. A generous runaway backstop
(never the normal path) catches only a livelocked/unidentifiable wait.
"""

from __future__ import annotations

import multiprocessing
import os
import threading
import time
from typing import Any

import pytest

from clio_agent.errors import LAUNCHER_CACHE_LOCK_TIMEOUT
from clio_agent.tools import launcher_cache_lock as lcl
from clio_agent.tools.launcher_cache_lock import (
    LauncherCacheLockTimeoutError,
    acquire_launcher_cache_lock,
    launcher_cache_lock_timeout_s,
    uses_shared_launcher_cache,
)
from clio_agent.tools.mcp_config import MCPServerSpec


def _mp_hold_lock_until_killed(cache_dir: str, ready_evt: Any, server_id: str) -> None:
    """Module-level (Windows-spawn-picklable) child-process target: hold the
    launcher-cache lock until this process is killed. Used to produce a
    GENUINELY dead holder (the OS releases the lock the instant the process
    is terminated) rather than a same-process fake, which Windows would not
    let this module force-delete out from under a still-open handle."""

    from clio_agent.tools import launcher_cache_lock as _lcl
    from clio_agent.tools import mcp_config as _mcp_config

    _mcp_config._mcp_uv_cache_dir = lambda: __import__("pathlib").Path(cache_dir)
    with _lcl.acquire_launcher_cache_lock(server_id, timeout_s=60.0):
        ready_evt.set()
        while True:
            time.sleep(0.05)


def test_default_timeout_is_a_positive_bound() -> None:
    assert launcher_cache_lock_timeout_s() > 0


def test_timeout_config_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLIO_MCP_LAUNCHER_CACHE_LOCK_TIMEOUT_S", "42")
    assert launcher_cache_lock_timeout_s() == 42.0


def test_uses_shared_launcher_cache_true_for_plain_stdio_spec() -> None:
    spec = MCPServerSpec(name="s", transport="stdio", command="uvx", args=("weather-mcp",))
    assert uses_shared_launcher_cache(spec) is True


def test_uses_shared_launcher_cache_false_for_explicit_uv_cache_dir() -> None:
    """A declaration with its OWN UV_CACHE_DIR opted out of the shared dir; never lock it."""

    spec = MCPServerSpec(
        name="s", transport="stdio", command="uvx", env={"UV_CACHE_DIR": "/custom/cache"}
    )
    assert uses_shared_launcher_cache(spec) is False


def test_uses_shared_launcher_cache_false_for_http() -> None:
    spec = MCPServerSpec(name="s", transport="http", url="https://example.com/mcp")
    assert uses_shared_launcher_cache(spec) is False


def test_acquire_and_release_succeeds_uncontended() -> None:
    with acquire_launcher_cache_lock("server-a", timeout_s=5.0):
        pass  # no exception


def test_acquire_is_serialized_against_a_concurrent_holder() -> None:
    """Two acquisitions on the SAME lock path never run concurrently (real filelock)."""

    order: list[str] = []
    release = threading.Event()

    def _hold() -> None:
        with acquire_launcher_cache_lock("server-a", timeout_s=5.0):
            order.append("first-acquired")
            release.wait(timeout=5.0)
            order.append("first-released")

    holder = threading.Thread(target=_hold, daemon=True)
    holder.start()
    # Give the holder a moment to actually acquire before we contend.
    deadline = time.time() + 2.0
    while "first-acquired" not in order and time.time() < deadline:
        time.sleep(0.01)
    assert "first-acquired" in order

    release.set()
    with acquire_launcher_cache_lock("server-a", timeout_s=5.0):
        order.append("second-acquired")
    holder.join(timeout=5.0)
    assert order.index("first-released") < order.index("second-acquired")


def test_lock_held_by_live_holder_is_waited_out() -> None:
    """SABOTAGE: a lock held by a LIVE process must be WAITED OUT, however slow --
    never raced against a deadline (owner ruling 2026-08-20, #1237). The holder
    here is a live thread in THIS process, so its recorded PID (our own) always
    reads alive."""

    release = threading.Event()
    order: list[str] = []

    def _hold() -> None:
        with acquire_launcher_cache_lock("server-b", timeout_s=5.0):
            order.append("held")
            release.wait(timeout=5.0)
            order.append("released")

    holder = threading.Thread(target=_hold, daemon=True)
    holder.start()
    deadline = time.time() + 2.0
    while "held" not in order and time.time() < deadline:
        time.sleep(0.01)
    assert "held" in order

    # A short runaway backstop would have raised well before the holder frees
    # the lock (0.6s < the holder's up-to-5s hold) -- proves the wait is driven
    # by the holder's liveness, not the bound, as long as the holder is alive.
    def _waiter() -> None:
        with acquire_launcher_cache_lock("server-b", timeout_s=5.0):
            order.append("second-acquired")

    started = time.monotonic()
    waiter = threading.Thread(target=_waiter, daemon=True)
    waiter.start()
    time.sleep(0.6)
    assert "second-acquired" not in order, "waiter acquired before the live holder released"
    release.set()
    holder.join(timeout=5.0)
    waiter.join(timeout=5.0)
    elapsed = time.monotonic() - started
    assert "second-acquired" in order
    assert order.index("released") < order.index("second-acquired")
    assert elapsed >= 0.6, "waiter returned before the holder released -- did not actually wait"


def test_dead_process_lock_is_acquired_promptly_without_the_backstop(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """A lock left by a GENUINELY dead process (killed while holding it, via a
    real child process) must be acquired promptly -- the OS itself already
    released it on process death, so this must never wait anywhere near the
    runway backstop."""

    from clio_agent.tools import mcp_config as _mcp_config

    monkeypatch.setattr(_mcp_config, "_mcp_uv_cache_dir", lambda: tmp_path)

    ctx = multiprocessing.get_context("spawn")
    ready_evt = ctx.Event()
    child = ctx.Process(
        target=_mp_hold_lock_until_killed, args=(str(tmp_path), ready_evt, "server-c")
    )
    child.start()
    try:
        assert ready_evt.wait(timeout=15.0), "child never acquired the lock"
        child.kill()
        child.join(timeout=10.0)
        assert not child.is_alive(), "child failed to die -- test cannot proceed"

        started = time.monotonic()
        with acquire_launcher_cache_lock("server-c", timeout_s=30.0):
            pass
        elapsed = time.monotonic() - started
        assert elapsed < 10.0, "acquisition waited near the backstop for an already-dead holder"
    finally:
        if child.is_alive():
            child.kill()
            child.join(timeout=5.0)


def test_dead_pid_owner_record_triggers_break_stale_lock(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """SABOTAGE: the decision branch (Timeout + a DEAD recorded owner PID) must
    call ``_break_stale_lock`` and then succeed on the immediate retry -- never
    fall through to waiting on the holder or burning the runaway backstop.
    Isolates the decision from real cross-process OS-lock contention (which
    Windows won't let this same-process test fake by deleting a still-open
    file) by forcing exactly ONE ``Timeout`` on the first acquire attempt."""

    from clio_agent.tools import mcp_config as _mcp_config

    monkeypatch.setattr(_mcp_config, "_mcp_uv_cache_dir", lambda: tmp_path)

    lock_path = lcl._lock_path()
    owner_path = lcl._owner_path(lock_path)
    dead_pid = 999_999_991
    owner_path.write_text(str(dead_pid), encoding="utf-8")

    broken: list[tuple[str, int]] = []
    real_break = lcl._break_stale_lock

    def _spy_break(lp: Any, op: Any, sid: str, pid: int) -> None:
        broken.append((sid, pid))
        real_break(lp, op, sid, pid)

    monkeypatch.setattr(lcl, "_break_stale_lock", _spy_break)

    from filelock import FileLock as _RealFileLock
    from filelock import Timeout

    original_acquire = _RealFileLock.acquire
    calls = {"n": 0}

    def _flaky_acquire(self: Any, *a: Any, **kw: Any) -> Any:
        calls["n"] += 1
        if calls["n"] == 1:
            raise Timeout(str(lock_path))
        return original_acquire(self, *a, **kw)

    monkeypatch.setattr(_RealFileLock, "acquire", _flaky_acquire)

    started = time.monotonic()
    with acquire_launcher_cache_lock("server-e", timeout_s=10.0):
        pass
    elapsed = time.monotonic() - started

    assert elapsed < 5.0
    assert broken == [("server-e", dead_pid)]
    assert not owner_path.exists()


def test_runaway_backstop_fires_typed_when_holder_unidentifiable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A lock held with NO readable owner record (this process can never verify
    liveness) still cannot wait forever -- the generous backstop is the
    last-resort catcher, and it fires typed."""

    monkeypatch.setattr(lcl, "_read_owner_pid", lambda _owner_path: None)

    release = threading.Event()

    def _hold_forever() -> None:
        with acquire_launcher_cache_lock("server-d", timeout_s=30.0):
            release.wait(timeout=10.0)

    holder = threading.Thread(target=_hold_forever, daemon=True)
    holder.start()
    time.sleep(0.2)

    started = time.monotonic()
    with pytest.raises(LauncherCacheLockTimeoutError) as exc_info:
        with acquire_launcher_cache_lock("server-d", timeout_s=0.5):
            pass
    elapsed = time.monotonic() - started
    assert elapsed < 3.0
    assert exc_info.value.server_id == "server-d"
    assert LAUNCHER_CACHE_LOCK_TIMEOUT in str(exc_info.value)

    release.set()
    holder.join(timeout=5.0)


def test_pid_alive_true_for_own_process() -> None:
    assert lcl._pid_alive(os.getpid()) is True


def test_pid_alive_false_for_implausible_pid() -> None:
    assert lcl._pid_alive(999_999_991) is False
