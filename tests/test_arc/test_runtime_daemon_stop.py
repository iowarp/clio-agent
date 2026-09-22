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
    assert runtime_stop._RUNTIME_STOP_STALL_SECONDS < 30.0


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
    monkeypatch.setattr(runtime_stop, "_RUNTIME_STOP_STALL_SECONDS", 0.05)
    monkeypatch.setattr(runtime_stop, "_RUNTIME_STOP_POLL_SECONDS", 0.01)
    killed: list[bool] = []
    monkeypatch.setattr(storage, "_kill_daemon_pidfile", lambda: killed.append(True))
    monkeypatch.setattr(storage, "_daemon_pidfile", lambda: tmp_path / "daemon.pid")

    outcome = runtime_stop.stop_runtime_daemon("", "error")

    assert outcome == runtime_stop.StopOutcome(stopped=False, path="stall_kill")
    assert calls == ["terminate", "wait:1.0"]
    assert killed == [True]


def test_stop_runtime_daemon_grace_polls_before_helper_exit_kill(
    fake_iowarp_core: types.SimpleNamespace,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A helper that exits 0 while the port still reads bound for a couple of
    polls must NOT be immediately hard-killed as ``helper_exit_kill`` -- the
    post-exit grace window should catch the ordinary case where the port
    frees a beat later than the helper's own exit."""

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
    # 1: main-loop check (alive) -> 2: 1st grace poll (alive) -> 3: 2nd grace
    # poll (freed) -> 4: the post-loop "confirm actually freed" re-check.
    assert calls["n"] == 4


def test_stop_runtime_daemon_reports_helper_exit_kill_when_grace_expires(
    fake_iowarp_core: types.SimpleNamespace,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the port never frees within the grace window, the run is genuinely
    ``helper_exit_kill`` and must still fall back to a hard kill."""

    class ExitedProcess:
        def poll(self) -> int:
            return 0

    monkeypatch.setattr(runtime_stop.subprocess, "Popen", lambda *a, **k: ExitedProcess())
    monkeypatch.setattr(runtime_stop, "_resolve_runtime_port", lambda config_path: 65001)
    monkeypatch.setattr(runtime_stop, "_runtime_alive", lambda port: True)
    monkeypatch.setattr(runtime_stop, "_HELPER_EXIT_GRACE_SECONDS", 0.03)
    monkeypatch.setattr(runtime_stop, "_RUNTIME_STOP_POLL_SECONDS", 0.01)
    killed: list[bool] = []
    monkeypatch.setattr(storage, "_kill_daemon_pidfile", lambda: killed.append(True))
    monkeypatch.setattr(storage, "_daemon_pidfile", lambda: tmp_path / "daemon.pid")

    outcome = runtime_stop.stop_runtime_daemon("", "error")

    assert outcome == runtime_stop.StopOutcome(stopped=False, path="helper_exit_kill")
    assert killed == [True]
