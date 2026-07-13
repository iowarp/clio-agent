"""Regression tests for the clio-core daemon clean-stop path (issue #765 (c)).

``_stop_runtime_daemon`` must build the ``clio_run stop`` command and its
environment with the SAME cross-platform helpers as the spawn path
(``_runtime_launcher_path`` for the ``.exe``-aware launcher name and
``_dynamic_library_env_var`` for the OS shared-library path variable),
otherwise the clean stop can never work on Windows/macOS and always falls
through to a hard kill of a storage engine.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import types
from pathlib import Path

import pytest

from clio_agent.arc import storage


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

    def fake_run(cmd, **kwargs):  # noqa: ANN001, ANN003 - test shim
        calls["cmd"] = cmd
        calls["env"] = kwargs["env"]
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(storage.subprocess, "run", fake_run)
    monkeypatch.setattr(storage, "_resolve_runtime_port", lambda config_path: 65001)
    monkeypatch.setattr(storage, "_runtime_alive", lambda port: False)

    def fail_kill() -> None:
        raise AssertionError("clean stop must not fall through to the pidfile kill")

    monkeypatch.setattr(storage, "_kill_daemon_pidfile", fail_kill)
    monkeypatch.setattr(storage, "_daemon_pidfile", lambda: tmp_path / "daemon.pid")

    storage._stop_runtime_daemon("", "error")

    expected_exe = storage._runtime_launcher_path(fake_iowarp_core)
    assert expected_exe is not None
    assert calls["cmd"] == [expected_exe, "stop"]

    lib_var = storage._dynamic_library_env_var()
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

    monkeypatch.setattr(storage.subprocess, "run", fail_run)
    monkeypatch.setattr(storage, "_resolve_runtime_port", lambda config_path: 65001)
    monkeypatch.setattr(storage, "_runtime_alive", lambda port: False)
    killed: list[bool] = []
    monkeypatch.setattr(storage, "_kill_daemon_pidfile", lambda: killed.append(True))
    monkeypatch.setattr(storage, "_daemon_pidfile", lambda: tmp_path / "daemon.pid")

    with caplog.at_level(logging.WARNING, logger=storage.logger.name):
        storage._stop_runtime_daemon("", "error")

    assert killed == [True]
    assert any("launcher_not_found" in record.getMessage() for record in caplog.records)
