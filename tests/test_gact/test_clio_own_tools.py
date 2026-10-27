"""CLIO runs its services with its own uv and Python; a host needs neither installed."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from clio_agent.gact.infrastructure import local_executor
from clio_agent.gact.infrastructure import native_service_platform as platform_helper
from clio_agent.gact.infrastructure.clio_python import on_clio_tools
from clio_agent.gact.infrastructure.models import CommandSpec, InfrastructureTarget

posix_only = pytest.mark.skipif(os.name == "nt", reason="POSIX layouts and sh")


def _executable(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\necho ok\n")
    path.chmod(0o755)
    return path


@pytest.fixture
def no_host_uv(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("GACT_BUNDLED_RUNTIME_DIR", raising=False)
    monkeypatch.delenv("CLIO_BIN_DIR", raising=False)
    monkeypatch.setattr(platform_helper.shutil, "which", lambda name: None)
    monkeypatch.setattr(platform_helper.Path, "home", lambda: tmp_path / "empty-home")


@posix_only
@pytest.mark.usefixtures("no_host_uv")
def test_the_desktop_runtime_uses_the_uv_it_ships(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Found live: the desktop ships gact-runtime/bin/uv, but services looked only on PATH."""
    runtime = tmp_path / "CLIO Desktop/gact-runtime"
    uv = _executable(runtime / "bin/uv")
    monkeypatch.setattr(sys, "executable", str(runtime / "python/bin/python3.14"))
    assert platform_helper.clio_tool("uv") == str(uv)


@posix_only
@pytest.mark.usefixtures("no_host_uv")
def test_a_deployed_install_uses_the_uv_in_its_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "clio/app"
    uv = _executable(root / "bin/uv")
    base = _executable(root / "uv-python/cpython-3.14/bin/python3.14")
    venv_python = root / "clio-agent/.venv/bin/python"
    venv_python.parent.mkdir(parents=True)
    venv_python.symlink_to(base)  # a venv's interpreter links elsewhere: not resolved
    monkeypatch.setattr(sys, "executable", str(venv_python))
    assert platform_helper.clio_tool("uv") == str(uv)


@posix_only
@pytest.mark.usefixtures("no_host_uv")
def test_the_launchers_bin_dir_then_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "executable", str(tmp_path / "python/bin/python3"))
    assert platform_helper.clio_tool("uv") is None
    uv = _executable(tmp_path / "launcher-bin/uv")
    monkeypatch.setenv("CLIO_BIN_DIR", str(uv.parent))
    assert platform_helper.clio_tool("uv") == str(uv)


def _target(**fields: object) -> InfrastructureTarget:
    return InfrastructureTarget.model_validate(
        {
            "id": "t",
            "label": "t",
            "kind": "ssh",
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
            **fields,
        }
    )


@posix_only
def test_a_remote_host_runs_the_python_and_uv_of_the_clio_installed_there(
    tmp_path: Path,
) -> None:
    """A minimal host has no python3, an HPC node's is too old: CLIO's own runs."""
    root = tmp_path / "custom root"
    for tool in ("clio-agent/.venv/bin/python", "bin/uv"):
        path = root / tool
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f'#!/bin/sh\necho "{tool} $*"\n')
        path.chmod(0o755)
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path / "home")}
    for program, expected in (("python3", "clio-agent/.venv/bin/python"), ("uv", "bin/uv")):
        spec = on_clio_tools(
            _target(install_root=str(root)), CommandSpec(program=program, args=["-c", "x y"])
        )
        assert spec.program == "sh"
        result = subprocess.run(
            [spec.program, *spec.args], capture_output=True, text=True, env=env, check=True
        )
        assert result.stdout.strip() == f"{expected} -c x y"


@posix_only
def test_without_a_clio_there_the_hosts_own_runs(tmp_path: Path) -> None:
    spec = on_clio_tools(_target(), CommandSpec(program="python3", args=["-c", "print(7)"]))
    env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path)}
    result = subprocess.run(
        [spec.program, *spec.args], capture_output=True, text=True, env=env, check=True
    )
    assert result.stdout.strip() == "7"


def test_other_programs_and_windows_hosts_are_left_alone() -> None:
    other = CommandSpec(program="bash", args=["-lc", "true"])
    assert on_clio_tools(_target(), other) is other
    windows = _target(ssh={"host": "h", "platform": "windows"})
    python = CommandSpec(program="python3", args=["-c", "x"])
    assert on_clio_tools(windows, python) is python


def test_local_commands_lead_path_with_clios_uv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    uv = _executable(tmp_path / "runtime/bin/uv")
    monkeypatch.setattr(local_executor, "clio_tool", lambda name: str(uv))
    env = local_executor.local_environment({"PATH": "/usr/bin:/bin"})
    assert str(uv.parent) in env["PATH"].split(os.pathsep)[:2]
