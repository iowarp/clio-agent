"""Regression tests for the clio-core daemon clean-stop path (issue #765 (c)).

``stop_runtime_daemon`` must build the ``clio_run stop`` command and its
environment with the SAME cross-platform helpers as the spawn path
(``_runtime_launcher_path`` for the ``.exe``-aware launcher name and
``_dynamic_library_env_var`` for the OS shared-library path variable),
otherwise the clean stop can never work on Windows/macOS and always falls
through to a hard kill of a storage engine.
"""

from __future__ import annotations

import logging
import os
import sys
import time
import types
from pathlib import Path

import pytest

from clio_agent.arc import runtime_stop, storage


@pytest.fixture()
def fake_iowarp_core(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> types.SimpleNamespace:
    """A fake ``iowarp_core`` module with a real platform-named launcher on disk."""
    bin_dir = tmp_path / "bin"
    lib_dir = tmp_path / "lib"
    bin_dir.mkdir()
    lib_dir.mkdir()
    launcher_name = "clio_run.exe" if sys.platform.startswith("win") else "clio_run"
    (bin_dir / launcher_name).write_bytes(b"")
    fake = types.SimpleNamespace(
        get_bin_dir=lambda: str(bin_dir),
        get_lib_dir=lambda: str(lib_dir),
    )
    monkeypatch.setitem(sys.modules, "iowarp_core", fake)
    return fake


def test_stop_runtime_daemon_uses_spawn_helpers(
    fake_iowarp_core: types.SimpleNamespace,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: dict[str, object] = {}

    class FakeProcess:
        def poll(self) -> int:
            return 0

    def fake_popen(cmd, **kwargs):  # noqa: ANN001, ANN003 - test shim
        calls["cmd"] = cmd
        calls["env"] = kwargs["env"]
        return FakeProcess()

    monkeypatch.setattr(runtime_stop.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(runtime_stop, "_resolve_runtime_port", lambda config_path: 65001)
    monkeypatch.setattr(runtime_stop, "_runtime_alive", lambda port: False)

    def fail_kill() -> None:
        raise AssertionError("clean stop must not fall through to the pidfile kill")

    monkeypatch.setattr(storage, "_kill_daemon_pidfile", fail_kill)
    monkeypatch.setattr(storage, "_daemon_pidfile", lambda: tmp_path / "daemon.pid")

    runtime_stop.stop_runtime_daemon("", "error")

    expected_exe = runtime_stop._runtime_launcher_path(fake_iowarp_core)
    assert expected_exe is not None
    assert calls["cmd"] == [expected_exe, "stop"]

    lib_var = runtime_stop._dynamic_library_env_var()
    env = calls["env"]
    assert isinstance(env, dict)
    assert env[lib_var].split(os.pathsep)[0] == fake_iowarp_core.get_lib_dir()


def test_stop_runtime_daemon_warns_and_kills_when_launcher_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    bin_dir = tmp_path / "empty-bin"
    bin_dir.mkdir()
    fake = types.SimpleNamespace(
        get_bin_dir=lambda: str(bin_dir),
        get_lib_dir=lambda: str(tmp_path / "lib"),
    )
    monkeypatch.setitem(sys.modules, "iowarp_core", fake)

    def fail_run(*args, **kwargs):  # noqa: ANN002, ANN003 - test shim
        raise AssertionError("no launcher on disk: clean stop must not be attempted")

    monkeypatch.setattr(runtime_stop.subprocess, "Popen", fail_run)
    monkeypatch.setattr(runtime_stop, "_resolve_runtime_port", lambda config_path: 65001)
    monkeypatch.setattr(runtime_stop, "_runtime_alive", lambda port: False)
    killed: list[bool] = []
    monkeypatch.setattr(storage, "_kill_daemon_pidfile", lambda: killed.append(True))
    monkeypatch.setattr(storage, "_daemon_pidfile", lambda: tmp_path / "daemon.pid")

    with caplog.at_level(logging.WARNING, logger=runtime_stop.logger.name):
        runtime_stop.stop_runtime_daemon("", "error")

    assert killed == [True]
    assert any("launcher_not_found" in record.getMessage() for record in caplog.records)


def test_stop_runtime_daemon_reaps_helper_as_soon_as_runtime_is_down(
    fake_iowarp_core: types.SimpleNamespace,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A hung stop helper must not hold Desktop Quit after the daemon has stopped."""

    calls: list[str] = []

    class HungStopProcess:
        def poll(self) -> None:
            return None

        def terminate(self) -> None:
            calls.append("terminate")

        def wait(self, *, timeout: float) -> int:
            calls.append(f"wait:{timeout}")
            return 0

        def kill(self) -> None:
            calls.append("kill")

    monkeypatch.setattr(runtime_stop.subprocess, "Popen", lambda *args, **kwargs: HungStopProcess())
    monkeypatch.setattr(runtime_stop, "_resolve_runtime_port", lambda config_path: 65001)
    monkeypatch.setattr(runtime_stop, "_runtime_alive", lambda port: False)
    monkeypatch.setattr(storage, "_kill_daemon_pidfile", lambda: calls.append("pidfile-kill"))
    monkeypatch.setattr(storage, "_daemon_pidfile", lambda: tmp_path / "daemon.pid")

    runtime_stop.stop_runtime_daemon("", "error")

    assert calls == ["terminate", "wait:1.0"]


def test_stop_stall_budget_fits_desktop_supervisor_window() -> None:
    """The desktop supervisor force-kills 30s after the 202 response

    (``GRACEFUL_SHUTDOWN_STALL`` in the Rust supervisor). This stop attempt is
    only ONE step inside that window -- the turn drain and agent-task executor
    joins run around it -- so its own stall budget must leave real headroom,
    not spend the whole 30s itself.
    """
    assert runtime_stop._DEFAULT_STOP_NO_PROGRESS_S < 30.0


def test_stop_outcome_reports_clean_stop(
    fake_iowarp_core: types.SimpleNamespace,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeProcess:
        def poll(self) -> int:
            return 0

    monkeypatch.setattr(runtime_stop.subprocess, "Popen", lambda *a, **k: FakeProcess())
    monkeypatch.setattr(runtime_stop, "_resolve_runtime_port", lambda config_path: 65001)
    monkeypatch.setattr(runtime_stop, "_runtime_alive", lambda port: False)
    monkeypatch.setattr(storage, "_kill_daemon_pidfile", lambda: pytest.fail("must not hard-kill"))
    monkeypatch.setattr(storage, "_daemon_pidfile", lambda: tmp_path / "daemon.pid")

    outcome = runtime_stop.stop_runtime_daemon("", "error")

    assert outcome == runtime_stop.StopOutcome(stopped=True, path="clean_stop")


def test_stop_outcome_reports_stall_kill(
    fake_iowarp_core: types.SimpleNamespace,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A helper that never exits and a runtime that never frees its port must
    report ``stall_kill`` once the (shortened, test-local) budget elapses."""

    calls: list[str] = []

    class NeverExitsProcess:
        def poll(self) -> None:
            return None

        def terminate(self) -> None:
            calls.append("terminate")

        def wait(self, *, timeout: float) -> int:
            calls.append(f"wait:{timeout}")
            return 0

        def kill(self) -> None:
            calls.append("kill")

    monkeypatch.setattr(runtime_stop.subprocess, "Popen", lambda *a, **k: NeverExitsProcess())
    monkeypatch.setattr(runtime_stop, "_resolve_runtime_port", lambda config_path: 65001)
    monkeypatch.setattr(runtime_stop, "_runtime_alive", lambda port: True)
    monkeypatch.setenv("CLIO_ARC_LIVENESS_STOP_NO_PROGRESS_S", "0.05")
    monkeypatch.setattr(runtime_stop, "_RUNTIME_STOP_POLL_SECONDS", 0.01)
    killed: list[bool] = []
    monkeypatch.setattr(storage, "_kill_daemon_pidfile", lambda: killed.append(True))
    monkeypatch.setattr(storage, "_daemon_pidfile", lambda: tmp_path / "daemon.pid")

    outcome = runtime_stop.stop_runtime_daemon("", "error")

    assert outcome == runtime_stop.StopOutcome(stopped=False, path="stall_kill")
    assert calls == ["terminate", "wait:1.0"]
    assert killed == [True]


def test_a_helper_exiting_while_the_daemon_still_stops_is_not_a_kill(
    fake_iowarp_core: types.SimpleNamespace,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The stop waits on the DAEMON: a helper that exits while the port still reads
    bound for a couple of polls is not a reason to kill."""

    class ExitedProcess:
        def poll(self) -> int:
            return 0

    calls = {"n": 0}

    def fake_alive(port: int) -> bool:
        calls["n"] += 1
        return calls["n"] < 3  # alive, alive, then freed on the 3rd poll

    monkeypatch.setattr(runtime_stop.subprocess, "Popen", lambda *a, **k: ExitedProcess())
    monkeypatch.setattr(runtime_stop, "_resolve_runtime_port", lambda config_path: 65001)
    monkeypatch.setattr(runtime_stop, "_runtime_alive", fake_alive)
    monkeypatch.setattr(runtime_stop, "_RUNTIME_STOP_POLL_SECONDS", 0.01)
    monkeypatch.setattr(storage, "_kill_daemon_pidfile", lambda: pytest.fail("must not hard-kill"))
    monkeypatch.setattr(storage, "_daemon_pidfile", lambda: tmp_path / "daemon.pid")

    outcome = runtime_stop.stop_runtime_daemon("", "error")

    assert outcome == runtime_stop.StopOutcome(stopped=True, path="clean_stop")


def test_a_busy_daemon_keeps_the_stop_waiting_past_the_slice(
    fake_iowarp_core: types.SimpleNamespace,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A durable daemon flushing on a slow disk takes longer than any fixed bound; while
    its work advances the stop keeps waiting -- no kill, no lost data."""

    class ExitedProcess:
        def poll(self) -> int:
            return 0

    from clio_agent.arc import daemon_progress

    alive = {"until": time.monotonic() + 0.4}  # frees after several stall slices
    work = {"w": 0.0}

    def advancing(pid: int) -> float:
        work["w"] += 1.0
        return work["w"]

    (tmp_path / "daemon.pid").write_text("4242 ", encoding="utf-8")
    monkeypatch.setattr(runtime_stop.subprocess, "Popen", lambda *a, **k: ExitedProcess())
    monkeypatch.setattr(runtime_stop, "_resolve_runtime_port", lambda config_path: 65001)
    monkeypatch.setattr(
        runtime_stop, "_runtime_alive", lambda port: time.monotonic() < alive["until"]
    )
    monkeypatch.setattr(
        runtime_stop, "_daemon_process_alive", lambda pid, ct: time.monotonic() < alive["until"]
    )
    monkeypatch.setenv("CLIO_ARC_LIVENESS_STOP_NO_PROGRESS_S", "0.05")
    monkeypatch.setattr(runtime_stop, "_RUNTIME_STOP_POLL_SECONDS", 0.01)
    monkeypatch.setattr(runtime_stop, "expect_daemon_exit", lambda pid: None)
    monkeypatch.setattr(daemon_progress, "process_work", advancing)
    monkeypatch.setattr(storage, "_kill_daemon_pidfile", lambda: pytest.fail("must not hard-kill"))
    monkeypatch.setattr(storage, "_daemon_pidfile", lambda: tmp_path / "daemon.pid")

    outcome = runtime_stop.stop_runtime_daemon("", "error")

    assert outcome == runtime_stop.StopOutcome(stopped=True, path="clean_stop")


def test_a_daemon_making_no_progress_is_killed_loudly(
    fake_iowarp_core: types.SimpleNamespace,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A whole slice with no daemon progress is a stall: killed, reported as such."""

    class ExitedProcess:
        def poll(self) -> int:
            return 0

    monkeypatch.setattr(runtime_stop.subprocess, "Popen", lambda *a, **k: ExitedProcess())
    monkeypatch.setattr(runtime_stop, "_resolve_runtime_port", lambda config_path: 65001)
    monkeypatch.setattr(runtime_stop, "_runtime_alive", lambda port: True)
    monkeypatch.setenv("CLIO_ARC_LIVENESS_STOP_NO_PROGRESS_S", "0.05")
    monkeypatch.setattr(runtime_stop, "_RUNTIME_STOP_POLL_SECONDS", 0.01)
    killed: list[bool] = []
    monkeypatch.setattr(storage, "_kill_daemon_pidfile", lambda: killed.append(True))
    monkeypatch.setattr(storage, "_daemon_pidfile", lambda: tmp_path / "daemon.pid")

    outcome = runtime_stop.stop_runtime_daemon("", "error")

    assert outcome == runtime_stop.StopOutcome(stopped=False, path="stall_kill")
    assert killed == [True]


def test_a_failed_stop_helper_is_not_waited_on_while_the_daemon_idles(
    fake_iowarp_core: types.SimpleNamespace,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``clio_run stop`` exiting non-zero (it could not load the config, so the daemon
    was never asked to stop) ends the wait at once, loudly, even while the daemon's
    own idle polling keeps its CPU time advancing (clio-core on Linux)."""

    class FailedProcess:
        def poll(self) -> int:
            return 1

    from clio_agent.arc import daemon_progress

    work = {"w": 0.0}

    def idle_polling(pid: int) -> float:
        work["w"] += 0.1
        return work["w"]

    (tmp_path / "daemon.pid").write_text("4242 ", encoding="utf-8")
    monkeypatch.setattr(runtime_stop.subprocess, "Popen", lambda *a, **k: FailedProcess())
    monkeypatch.setattr(runtime_stop, "_resolve_runtime_port", lambda config_path: 65001)
    monkeypatch.setattr(runtime_stop, "_runtime_alive", lambda port: True)
    monkeypatch.setattr(runtime_stop, "expect_daemon_exit", lambda pid: None)
    monkeypatch.setattr(daemon_progress, "process_work", idle_polling)
    killed: list[bool] = []
    monkeypatch.setattr(storage, "_kill_daemon_pidfile", lambda: killed.append(True))
    monkeypatch.setattr(storage, "_daemon_pidfile", lambda: tmp_path / "daemon.pid")

    with caplog.at_level(logging.WARNING, logger=runtime_stop.logger.name):
        outcome = runtime_stop.stop_runtime_daemon("", "error")

    assert outcome == runtime_stop.StopOutcome(stopped=False, path="helper_failed_kill")
    assert killed == [True]
    assert any("exited with code 1" in record.getMessage() for record in caplog.records)


def test_the_stop_waits_a_long_stretch_without_progress_before_a_kill() -> None:
    """Not 3 s: a daemon flushing to a slow disk pauses between writes; the no-progress
    stretch is long, yet inside the desktop supervisor's 30 s graceful window."""
    assert 10.0 <= runtime_stop._DEFAULT_STOP_NO_PROGRESS_S < 30.0


def test_a_pidfile_kill_that_fails_is_reported_and_keeps_the_pidfile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A kill the OS refuses (AccessDenied) is loud and typed, never ``except: pass``; the
    pidfile is kept so the next stop can still find the daemon.

    **Sabotage:** restore ``except Exception: pass`` -> no error record, pidfile unlinked.
    """
    import subprocess

    import psutil

    daemon = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        pidfile = tmp_path / "daemon.pid"
        ctime = psutil.Process(daemon.pid).create_time()
        pidfile.write_text(f"{daemon.pid} {ctime!r}", encoding="utf-8")
        monkeypatch.setattr(storage, "_daemon_pidfile", lambda: pidfile)

        def denied(self: psutil.Process) -> None:
            raise psutil.AccessDenied(self.pid)

        monkeypatch.setattr(psutil.Process, "terminate", denied)
        with caplog.at_level(logging.ERROR, logger=runtime_stop.logger.name):
            runtime_stop.kill_daemon_pidfile()

        assert runtime_stop.DAEMON_KILL_FAILED in caplog.text
        assert pidfile.exists()
        assert daemon.poll() is None
    finally:
        daemon.kill()
        daemon.wait(timeout=10)


def test_the_stop_no_progress_window_is_configurable(monkeypatch: pytest.MonkeyPatch) -> None:
    """``arc.liveness.stop_no_progress_s`` sets the stretch; a non-positive value keeps 15 s."""
    monkeypatch.setenv("CLIO_ARC_LIVENESS_STOP_NO_PROGRESS_S", "2.5")
    assert runtime_stop.stop_no_progress_s() == 2.5
    monkeypatch.setenv("CLIO_ARC_LIVENESS_STOP_NO_PROGRESS_S", "0")
    assert runtime_stop.stop_no_progress_s() == runtime_stop._DEFAULT_STOP_NO_PROGRESS_S


_SILENT_LISTENER = (
    "import socket, sys, time\n"
    "s = socket.socket()\n"
    "s.bind(('127.0.0.1', 0))\n"
    "s.listen(0)\n"
    "print(s.getsockname()[1], flush=True)\n"
    "time.sleep(600)\n"
)


def test_a_suspended_daemon_whose_port_stops_answering_is_killed_not_a_clean_stop(
    fake_iowarp_core: types.SimpleNamespace,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A daemon that stopped running keeps its listener; once the accept backlog is full
    a connect probe times out exactly like a freed port. The stop must not take that
    for a clean stop (which left the daemon running with its pidfile gone): the process
    is still there and makes no progress, so it is killed, loudly.

    **Sabotage:** decide the clean stop on the port probe alone -> ``clean_stop``, and the
    suspended process survives the stop. Drop the resume after the SIGTERM (POSIX) -> the
    stopped process ignores it for the whole grace and needs the SIGKILL.
    """
    import socket
    import subprocess

    import psutil

    class ExitedProcess:  # ``clio_run stop`` delivered its request and exited
        def poll(self) -> int:
            return 0

    daemon = subprocess.Popen(
        [sys.executable, "-c", _SILENT_LISTENER], stdout=subprocess.PIPE, text=True
    )
    try:
        assert daemon.stdout is not None
        port = int(daemon.stdout.readline())
        proc = psutil.Process(daemon.pid)
        proc.suspend()
        held: list[socket.socket] = []
        # Fill the accept backlog the suspended process never drains, until a probe fails.
        while runtime_stop._runtime_alive(port):
            assert len(held) < 64, "the accept backlog never filled"
            try:
                held.append(socket.create_connection(("127.0.0.1", port), timeout=0.5))
            except OSError:
                break  # full: the probe in the loop condition confirms it
        assert not runtime_stop._runtime_alive(port)

        pidfile = tmp_path / "daemon.pid"
        pidfile.write_text(f"{daemon.pid} {proc.create_time()!r}", encoding="utf-8")
        monkeypatch.setattr(storage, "_daemon_pidfile", lambda: pidfile)
        monkeypatch.setattr(runtime_stop.subprocess, "Popen", lambda *a, **k: ExitedProcess())
        monkeypatch.setattr(runtime_stop, "_resolve_runtime_port", lambda config_path: port)
        monkeypatch.setenv("CLIO_ARC_LIVENESS_STOP_NO_PROGRESS_S", "1.5")
        sigkills: list[int] = []
        real_kill = psutil.Process.kill

        def recording_kill(self: psutil.Process) -> None:
            sigkills.append(self.pid)
            real_kill(self)

        monkeypatch.setattr(psutil.Process, "kill", recording_kill)

        outcome = runtime_stop.stop_runtime_daemon("", "error")

        assert outcome == runtime_stop.StopOutcome(stopped=False, path="stall_kill")
        assert daemon.wait(timeout=10) is not None  # killed, not left running
        assert not pidfile.exists()
        # The terminate took (a suspended process is continued so it can act on it); the
        # SIGKILL after the whole grace was never needed.
        assert sigkills == []
        for sock in held:
            sock.close()
    finally:
        if daemon.poll() is None:
            psutil.Process(daemon.pid).resume()
            daemon.kill()
            daemon.wait(timeout=10)
