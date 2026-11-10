"""An idle ``run_code`` kernel is reaped after ``code.idle_minutes``; a running one is not (TD15)."""

from __future__ import annotations

import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from clio_agent.gact.code_mode import kernels


def _kernel(key: tuple[str, str], idle_s: float, stopped: list[Any]) -> SimpleNamespace:
    kernel = SimpleNamespace(key=key, last_used=time.monotonic() - idle_s, lock=threading.Lock())
    kernel.stop = lambda reason: stopped.append((key, reason))
    return kernel


def test_idle_kernels_are_stopped_and_busy_or_recent_ones_kept(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SABOTAGE: drop the idle-time check in ``reap_idle`` and the recent kernel is stopped."""
    monkeypatch.setenv("CLIO_CODE_IDLE_MINUTES", "30")
    stopped: list[Any] = []
    old = _kernel(("s1", "main"), 31 * 60, stopped)
    busy = _kernel(("s2", "main"), 31 * 60, stopped)
    recent = _kernel(("s3", "main"), 60, stopped)
    busy.lock.acquire()  # a script is running in it
    monkeypatch.setattr(kernels, "_KERNELS", {k.key: k for k in (old, busy, recent)}, raising=True)
    try:
        reaped = kernels.reap_idle()
    finally:
        busy.lock.release()
    assert reaped == [old]
    assert stopped == [(("s1", "main"), "idle")]
    assert set(kernels._KERNELS) == {("s2", "main"), ("s3", "main")}


def test_a_kernel_outlives_the_thread_that_started_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ``pdeathsig`` wrapper kills a child when its starting *thread* exits; kernels
    start on a long-lived thread, so a kernel started in one turn serves the next (T12).
    SABOTAGE: make ``_spawn`` call ``subprocess.Popen`` directly and this fails."""
    import shutil
    import threading
    import time

    from clio_agent.gact.code_mode import kernels

    if shutil.which("setpriv") is None:
        pytest.skip("setpriv (pdeathsig) is POSIX-only")
    monkeypatch.setenv("CLIO_ALLOWED_ROOTS", str(tmp_path))
    started: list[Any] = []
    turn = threading.Thread(target=lambda: started.append(kernels.kernel_for("s", "k", tmp_path)))
    turn.start()
    turn.join()
    try:
        (kernel,) = started
        time.sleep(1.0)
        assert kernel.alive(), f"the kernel died with its thread: {kernel.diagnostics()}"
        assert kernels.kernel_for("s", "k", tmp_path) is kernel
    finally:
        kernels.close_session("s")
