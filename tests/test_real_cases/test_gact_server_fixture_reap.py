"""Unit coverage for the ``gact_server`` fixture's process-tree reap (conftest.py).

Failing-first regression for the Windows-fatal teardown bug: the pre-fix
``_reap_process_group`` called ``os.getpgid``/``os.killpg`` unconditionally, and
neither exists on Windows at all — every live real-case test (earthscope,
wildfire, case07) raised ``AttributeError`` at fixture teardown on native
Windows. These tests exercise the reap helper directly against a real,
disposable dummy process tree (no live gact server, no provider, no network),
so they run in the default offline subset on every platform and prove the
reaper actually kills the whole tree on whichever platform they run under.
"""

from __future__ import annotations

import contextlib
import subprocess
import sys
import time
from pathlib import Path

import psutil
import pytest

from .conftest import _reap_process_group

_DUMMY_TREE_SCRIPT = """\
import subprocess
import sys
import time

pidfile = sys.argv[1]
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
with open(pidfile, "w", encoding="utf-8") as f:
    f.write(str(child.pid))
time.sleep(120)
"""


def _wait_for_pidfile(pidfile: Path, *, deadline: float) -> int:
    """Poll ``pidfile`` for the grandchild pid the dummy script writes."""
    while time.monotonic() < deadline:
        try:
            text = pidfile.read_text(encoding="utf-8").strip()
        except OSError:
            text = ""
        if text:
            return int(text)
        time.sleep(0.05)
    pytest.fail(f"dummy child never wrote its pid to {pidfile}")


def _wait_until(predicate, *, deadline: float, interval: float = 0.1) -> bool:
    """Poll ``predicate`` until it's true or ``deadline`` passes."""
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def test_reap_process_group_kills_whole_dummy_tree(tmp_path: Path) -> None:
    """The reaper must kill the spawned process AND its child (the MCP-child analogue).

    Spawns a two-level dummy tree with the SAME spawn kwarg the fixture uses
    (``start_new_session=True`` — see the ``gact_server`` fixture's Popen
    call) — a parent that spawns one grandchild and writes its pid out, then
    both sleep. Calls :func:`_reap_process_group` exactly as the fixture's
    teardown does and asserts both levels are gone afterward. Before the fix,
    this call raised ``AttributeError: module 'os' has no attribute
    'getpgid'`` on Windows (proving the bug); on POSIX the equivalent path
    already worked (proving no regression).
    """
    script = tmp_path / "dummy_tree.py"
    script.write_text(_DUMMY_TREE_SCRIPT, encoding="utf-8")
    pidfile = tmp_path / "grandchild.pid"

    process = subprocess.Popen(
        [sys.executable, str(script), str(pidfile)],
        start_new_session=True,
    )
    grandchild_pid: int | None = None
    try:
        grandchild_pid = _wait_for_pidfile(pidfile, deadline=time.monotonic() + 10.0)
        assert psutil.pid_exists(grandchild_pid), "dummy grandchild never started"
        grandchild_create_time = psutil.Process(grandchild_pid).create_time()
        assert process.poll() is None, "dummy parent exited before the reap could be tested"

        _reap_process_group(process, timeout=5.0)

        assert process.poll() is not None, "reaper did not terminate the parent process"

        def _grandchild_gone() -> bool:
            if not psutil.pid_exists(grandchild_pid):
                return True
            # PID-reuse guard: a different process may already have recycled the pid.
            try:
                return psutil.Process(grandchild_pid).create_time() != grandchild_create_time
            except psutil.NoSuchProcess:
                return True

        assert _wait_until(_grandchild_gone, deadline=time.monotonic() + 5.0), (
            "reaper left the grandchild (MCP-child analogue) running — only the top "
            "PID was reaped, reproducing the cross-cell process leak this fixture "
            "exists to prevent"
        )
    finally:
        # Safety net: never leak a live process out of this test even if an assertion
        # above failed mid-way.
        if process.poll() is None:
            with contextlib.suppress(Exception):
                process.kill()
            with contextlib.suppress(subprocess.TimeoutExpired):
                process.wait(timeout=5.0)
        if grandchild_pid is not None:
            with contextlib.suppress(psutil.NoSuchProcess, psutil.AccessDenied):
                psutil.Process(grandchild_pid).kill()


def test_reap_process_group_is_a_noop_on_an_already_exited_process(tmp_path: Path) -> None:
    """Reaping a process that already exited must return immediately, not raise.

    Covers the ``process.poll() is not None`` early-return — the common case
    where a cell's server crashed or was killed before teardown runs.
    """
    process = subprocess.Popen(
        [sys.executable, "-c", "pass"],
        start_new_session=True,
    )
    process.wait(timeout=10.0)
    assert process.poll() is not None

    _reap_process_group(process, timeout=5.0)  # must not raise
