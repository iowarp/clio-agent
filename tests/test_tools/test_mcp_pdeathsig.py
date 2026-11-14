"""MCP stdio children must die with the clio server (no orphan pile-up).

clio's process-group teardown reaps MCP subprocess children on a *graceful* stop
(`clio stop`, TUI close, test fixture teardown). These tests cover the remaining
edge — a *hard* death of the clio server (SIGKILL / OOM-killer / crash) where no
teardown runs — via the kernel parent-death signal set by
``pdeathsig_wrapped_command`` (``setpriv --pdeathsig SIGKILL``). Without it, the
``uvx`` MCP children orphan to init and accumulate.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from clio_agent.tools.mcp_config import pdeathsig_wrapped_command

# How long to wait for the process states this test drives. These bounds only catch a
# broken wrapper (which never reaps) and are not a speed claim: a loaded runner can take
# seconds to schedule a SIGKILLed child's exit. The child sleeps longer, so its own exit
# can never pass for a reap.
_SETTLE_S = 60.0
_CHILD_SLEEP_S = 300


def _comm(pid: int) -> str:
    """The Linux process name (``/proc/<pid>/comm``), or ``""`` once it is gone."""
    try:
        with open(f"/proc/{pid}/comm", encoding="utf-8") as handle:
            return handle.read().strip()
    except OSError:
        return ""


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # A killed child whose adoptive parent has not reaped it yet is a zombie: dead, but
    # still signalable. On a loaded runner that reap can lag the kill.
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as handle:
            return handle.read().rsplit(")", 1)[1].split()[0] != "Z"
    except OSError:
        return False  # gone between the two checks


def test_wrap_is_passthrough_off_linux(monkeypatch: pytest.MonkeyPatch) -> None:
    """No setpriv on non-Linux — return the command unchanged (process-group
    teardown remains the mechanism there)."""
    monkeypatch.setattr(sys, "platform", "darwin")
    assert pdeathsig_wrapped_command("uvx", ["geo-mcp"]) == ("uvx", ["geo-mcp"])


def test_wrap_is_passthrough_without_setpriv(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr("clio_agent.tools.mcp_config.shutil.which", lambda _name: None)
    assert pdeathsig_wrapped_command("uvx", ["geo-mcp"]) == ("uvx", ["geo-mcp"])


def test_wrap_prepends_setpriv_pdeathsig_on_linux(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(
        "clio_agent.tools.mcp_config.shutil.which", lambda _name: "/usr/bin/setpriv"
    )
    cmd, args = pdeathsig_wrapped_command("uvx", ["geo-mcp", "--flag"])
    assert cmd == "/usr/bin/setpriv"
    assert args == ["--pdeathsig", "SIGKILL", "--", "uvx", "geo-mcp", "--flag"]


@pytest.mark.skipif(
    sys.platform != "linux" or not shutil.which("setpriv"),
    reason="parent-death-signal reaping needs Linux + setpriv",
)
def test_pdeathsig_reaps_child_on_hard_parent_kill(tmp_path: Path) -> None:
    """The leak-detector: spawn a child the same way the mcp SDK does (asyncio
    subprocess), HARD-kill the parent so no cleanup runs, and assert the wrapped
    child is reaped by the kernel. A plain (unwrapped) child would orphan to init —
    which is the leak this guards against."""
    pidfile = tmp_path / "child.pid"
    parent_src = (
        "import asyncio\n"
        "from pathlib import Path\n"
        "async def main():\n"
        "    p = await asyncio.create_subprocess_exec("
        f"'setpriv','--pdeathsig','SIGKILL','--','sleep','{_CHILD_SLEEP_S}')\n"
        # Publish readiness only after the complete PID has been written. A
        # reader can observe an empty file between open('w') and write().
        f"    pending = Path({str(pidfile.with_suffix('.tmp'))!r})\n"
        "    pending.write_text(str(p.pid), encoding='utf-8')\n"
        f"    pending.replace({str(pidfile)!r})\n"
        f"    await asyncio.sleep({_CHILD_SLEEP_S})\n"
        "asyncio.run(main())\n"
    )
    parent = subprocess.Popen([sys.executable, "-c", parent_src])
    try:
        deadline = time.monotonic() + _SETTLE_S
        while not pidfile.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert pidfile.exists(), "parent never spawned the child"
        child_pid = int(pidfile.read_text())
        assert _alive(child_pid), "child should run while the parent is alive"
        # The pid is written as soon as ``setpriv`` starts, but setpriv arms the death
        # signal (prctl) only just before it execs ``sleep``: a kill inside that window
        # would orphan the child whatever the wrapper does. Waiting for the exec proves
        # the signal is armed (this failed once on a loaded 4-worker CI runner).
        deadline = time.monotonic() + _SETTLE_S
        while _comm(child_pid) != "sleep" and time.monotonic() < deadline:
            time.sleep(0.02)
        assert _comm(child_pid) == "sleep", "setpriv never exec'd the wrapped child"

        parent.kill()  # SIGKILL the parent: no graceful cleanup can run
        parent.wait(timeout=_SETTLE_S)

        # The parent's exit path has queued the death signal by the time wait() returns;
        # poll until the child has died rather than timing how fast the runner kills it.
        deadline = time.monotonic() + _SETTLE_S
        while _alive(child_pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not _alive(child_pid), (
            "MCP child orphaned after a hard parent kill — pdeathsig wrapper failed"
        )
    finally:
        if parent.poll() is None:
            parent.kill()
        if pidfile.exists():
            try:
                os.kill(int(pidfile.read_text()), 9)
            except (ProcessLookupError, ValueError):
                pass
