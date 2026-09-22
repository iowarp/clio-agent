"""The clio-core daemon spawn must survive a Job Object that forbids breakaway.

``CREATE_BREAKAWAY_FROM_JOB`` (#900) exists so the shared daemon outlives a
server hard-kill. It is free only when no Job Object is assigned, or when the
assigned one permits breakaway. Inside a FOREIGN job without ``BREAKAWAY_OK``
-- a CI runner, a sandbox, a managed Windows desktop -- ``CreateProcess``
refuses the flag with ``ERROR_ACCESS_DENIED`` and the daemon never starts, so
ARC degrades to LocalFS on a machine where clio-core is perfectly healthy.

That is what cost v0.9.4.1 its Windows bundled installer: the build's own ARC
proof ran inside the runner's job object and could not spawn ``clio_run.exe``.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from clio_agent.arc import storage
from clio_agent.arc.runtime_spawn import _detached_popen_kwargs

CREATE_BREAKAWAY_FROM_JOB = 0x01000000


def test_breakaway_is_requested_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """The #900 property is still the default: ask to leave the job."""
    monkeypatch.setattr("clio_agent.arc.runtime_spawn.sys.platform", "win32")
    flags = _detached_popen_kwargs()["creationflags"]
    assert flags & CREATE_BREAKAWAY_FROM_JOB


def test_breakaway_can_be_dropped(monkeypatch: pytest.MonkeyPatch) -> None:
    """``breakaway=False`` drops only that flag, keeping the rest of the detach."""
    monkeypatch.setattr("clio_agent.arc.runtime_spawn.sys.platform", "win32")
    full = _detached_popen_kwargs()["creationflags"]
    reduced = _detached_popen_kwargs(breakaway=False)["creationflags"]
    assert not reduced & CREATE_BREAKAWAY_FROM_JOB
    assert reduced == full & ~CREATE_BREAKAWAY_FROM_JOB
    assert reduced != 0, "dropping breakaway must not drop the whole detach"


def test_posix_is_untouched_by_the_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    """There is no Job Object on POSIX; both calls yield the same session detach."""
    monkeypatch.setattr("clio_agent.arc.runtime_spawn.sys.platform", "linux")
    assert _detached_popen_kwargs() == _detached_popen_kwargs(breakaway=False)
    assert _detached_popen_kwargs()["start_new_session"] is True


class _FakeProc:
    pid = 4321


def _spawn_with_popen(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, popen: Any) -> None:
    """Drive ``_spawn_runtime_daemon`` with a stubbed Popen and no real daemon."""
    monkeypatch.setattr("clio_agent.arc.storage.sys.platform", "win32")
    monkeypatch.setattr("clio_agent.arc.storage.runtime_state_dir", lambda: tmp_path)
    monkeypatch.setattr("clio_agent.arc.storage.clear_crash_record", lambda _dir: None)
    monkeypatch.setattr(
        "clio_agent.arc.storage.watch_daemon_process",
        lambda _proc, log_path, state_dir: None,
    )
    monkeypatch.setattr("clio_agent.arc.storage._proc_create_time", lambda _pid: 1.0)
    monkeypatch.setattr(
        "clio_agent.arc.storage._daemon_pidfile", lambda: tmp_path / "clio-runtime.pid"
    )
    monkeypatch.setattr(
        "clio_agent.arc.storage._runtime_launcher_path",
        lambda _core: str(tmp_path / "clio_run.exe"),
    )
    monkeypatch.setattr("clio_agent.arc.storage.subprocess.Popen", popen)

    class _Core:
        @staticmethod
        def get_lib_dir() -> str:
            return str(tmp_path)

        @staticmethod
        def get_bin_dir() -> str:
            return str(tmp_path)

    storage._spawn_runtime_daemon(_Core(), "", "error")


def test_spawn_retries_without_breakaway_when_the_job_denies_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A denied breakaway must yield an attached daemon, not a LocalFS degrade."""
    attempts: list[int] = []

    def popen(_args: Any, **kwargs: Any) -> _FakeProc:
        flags = kwargs["creationflags"]
        attempts.append(flags)
        if flags & CREATE_BREAKAWAY_FROM_JOB:
            raise PermissionError(5, "Access is denied")
        return _FakeProc()

    with caplog.at_level("WARNING", logger="clio_agent.arc.storage"):
        _spawn_with_popen(monkeypatch, tmp_path, popen)

    assert len(attempts) == 2, "expected one breakaway attempt then one attached retry"
    assert attempts[0] & CREATE_BREAKAWAY_FROM_JOB
    assert not attempts[1] & CREATE_BREAKAWAY_FROM_JOB
    # The downgrade costs the #900 survive-a-hard-kill property, so it is reported.
    assert "job_object_breakaway_denied" in caplog.text


def test_spawn_does_not_retry_when_breakaway_is_allowed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The healthy path stays one attempt, with no downgrade reported."""
    attempts: list[int] = []

    def popen(_args: Any, **kwargs: Any) -> _FakeProc:
        attempts.append(kwargs["creationflags"])
        return _FakeProc()

    with caplog.at_level("WARNING", logger="clio_agent.arc.storage"):
        _spawn_with_popen(monkeypatch, tmp_path, popen)

    assert len(attempts) == 1
    assert attempts[0] & CREATE_BREAKAWAY_FROM_JOB
    assert "job_object_breakaway_denied" not in caplog.text


def test_a_real_permission_error_still_propagates_on_posix(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Off Windows a denial is a genuine failure, never a flag to drop."""

    def popen(_args: Any, **_kwargs: Any) -> _FakeProc:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr("clio_agent.arc.storage.sys.platform", "linux")
    monkeypatch.setattr("clio_agent.arc.storage.runtime_state_dir", lambda: tmp_path)
    monkeypatch.setattr("clio_agent.arc.storage.clear_crash_record", lambda _dir: None)
    monkeypatch.setattr(
        "clio_agent.arc.storage._runtime_launcher_path", lambda _core: str(tmp_path / "clio_run")
    )
    monkeypatch.setattr("clio_agent.arc.storage.subprocess.Popen", popen)

    class _Core:
        @staticmethod
        def get_lib_dir() -> str:
            return str(tmp_path)

        @staticmethod
        def get_bin_dir() -> str:
            return str(tmp_path)

    with pytest.raises(PermissionError):
        storage._spawn_runtime_daemon(_Core(), "", "error")


def test_subprocess_exposes_the_flag_we_mask() -> None:
    """Guard the literal against a stdlib rename."""
    assert getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", CREATE_BREAKAWAY_FROM_JOB) == (
        CREATE_BREAKAWAY_FROM_JOB
    )
