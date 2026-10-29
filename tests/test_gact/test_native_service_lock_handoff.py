"""Native startup can hand its lifecycle lock to a worker while readiness observes."""

from __future__ import annotations

import errno
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from clio_agent.gact.infrastructure import native_service_platform as platform
from clio_agent.gact.infrastructure import node_service


def test_detached_waiter_survives_more_than_the_old_crt_retry_window(tmp_path: Path) -> None:
    root = tmp_path / "service"
    root.mkdir()
    script = """import importlib.util, pathlib, sys
spec = importlib.util.spec_from_file_location('service_platform', sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
root = pathlib.Path(sys.argv[2])
(root / 'waiting').write_text('ready')
with module.locked(root):
    print('acquired', flush=True)
"""
    process: subprocess.Popen[str] | None = None
    try:
        with platform.locked(root):
            process = subprocess.Popen(
                [sys.executable, "-B", "-c", script, str(Path(platform.__file__)), str(root)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            deadline = time.monotonic() + 10
            while not (root / "waiting").exists() and time.monotonic() < deadline:
                assert process.poll() is None
                time.sleep(0.05)
            assert (root / "waiting").is_file()
            # The CRT LK_LOCK used to terminate the worker after ten retries.
            time.sleep(11.2)
            assert process.poll() is None, "a contended waiter died before handoff"
        stdout, stderr = process.communicate(timeout=15)
        assert process.returncode == 0, stderr
        assert stdout.strip() == "acquired"
        if sys.platform == "win32":
            assert '"event": "lifecycle_lock_wait"' in stderr
            assert '"event": "lifecycle_lock_acquired"' in stderr
        # A settled worker released the lock rather than leaving lifecycle state wedged.
        with platform.locked(root):
            pass
    finally:
        if process is not None and process.poll() is None:
            process.kill()
            process.communicate(timeout=10)


def test_windows_lock_wait_is_bounded_and_preserves_non_contention_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempts: list[int] = []

    def contended(descriptor: int, mode: int, count: int) -> None:
        attempts.append(mode)
        raise OSError(errno.EACCES, "lock held")

    module = SimpleNamespace(LK_NBLCK=2, locking=contended)
    monkeypatch.setitem(sys.modules, "msvcrt", module)
    monkeypatch.setattr(platform, "WINDOWS_LOCK_WAIT_SECONDS", 0.08)
    with pytest.raises(TimeoutError, match="lifecycle lock timed out") as caught:
        platform._acquire_windows_lock(123, tmp_path / "lock")
    assert caught.value.__cause__.errno == errno.EACCES
    assert len(attempts) >= 2 and set(attempts) == {module.LK_NBLCK}

    def invalid(descriptor: int, mode: int, count: int) -> None:
        raise OSError(errno.EBADF, "invalid descriptor")

    module.locking = invalid
    with pytest.raises(OSError) as caught:
        platform._acquire_windows_lock(123, tmp_path / "lock")
    assert caught.value.errno == errno.EBADF


def test_start_releases_lifecycle_lock_before_http_and_status_never_takes_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "service"
    root.mkdir()
    held = False
    locks = 0

    @contextmanager
    def locked(path: Path) -> Iterator[None]:
        nonlocal held, locks
        locks += 1
        held = True
        try:
            yield
        finally:
            held = False

    def launch(path: Path, request: dict[str, Any]) -> None:
        assert held, "generation and receipt publication must remain serialized"

    def observation(path: Path, **kwargs: Any) -> dict[str, Any]:
        assert not held, "HTTP readiness must not block the worker's lifecycle lock"
        return {"serving": False, "worker_alive": True}

    monkeypatch.setattr(node_service, "owner", lambda *args: None)
    monkeypatch.setattr(node_service, "locked", locked)
    monkeypatch.setattr(node_service, "launch", launch)
    monkeypatch.setattr(node_service, "observation", observation)
    request = {"root": str(root), "owner": "test-owner", "action": "start"}
    assert node_service.control(request)["worker_alive"]
    assert locks == 1
    assert node_service.control({**request, "action": "status"})["worker_alive"]
    assert locks == 1
