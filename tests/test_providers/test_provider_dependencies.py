"""Tests for self-repairing optional provider dependencies."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest

from clio_agent.providers import dependencies

pytestmark = pytest.mark.real_dependency_installer


def test_argonne_support_is_a_noop_when_globus_is_available(monkeypatch: Any) -> None:
    """An already prepared backend must not invoke a package installer."""

    monkeypatch.setattr(dependencies, "_module_available", lambda _name: True)
    run = Mock()
    monkeypatch.setattr(subprocess, "run", run)

    assert dependencies.ensure_argonne_support() is False
    run.assert_not_called()


def test_argonne_support_installs_with_bundled_uv_without_a_console(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    """The desktop runtime's bundled uv installs into its own interpreter."""

    runtime = tmp_path / "runtime"
    python = runtime / "python" / ("python.exe" if os.name == "nt" else "python")
    uv = runtime / "bin" / ("uv.exe" if os.name == "nt" else "uv")
    python.parent.mkdir(parents=True)
    uv.parent.mkdir(parents=True)
    python.touch()
    uv.touch()
    availability = iter((False, False, True))
    calls: list[tuple[list[str], dict[str, object]]] = []

    monkeypatch.setattr(dependencies, "_module_available", lambda _name: next(availability))

    def _run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, "installed", "")

    monkeypatch.setattr(subprocess, "run", _run)

    assert dependencies.ensure_argonne_support(python_executable=str(python)) is True
    assert calls[0][0] == [
        str(uv),
        "pip",
        "install",
        "--python",
        str(python),
        "globus-sdk>=3.0.0",
    ]
    assert calls[0][1]["capture_output"] is True
    if os.name == "nt":
        assert calls[0][1]["creationflags"] == subprocess.CREATE_NO_WINDOW


def test_argonne_support_reports_the_installer_failure(monkeypatch: Any) -> None:
    """Installation failures remain actionable instead of claiming sign-in opened."""

    monkeypatch.setattr(dependencies, "_module_available", lambda _name: False)
    monkeypatch.setattr(dependencies, "_uv_executable", lambda _python: "uv")
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda command, **_kwargs: subprocess.CompletedProcess(command, 1, "", "permission denied"),
    )

    with pytest.raises(dependencies.ProviderDependencyInstallError, match="permission denied"):
        dependencies.ensure_argonne_support(python_executable="python")


def test_claude_code_support_installs_the_sdk_into_the_active_runtime(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """Claude repair uses the fixed SDK requirement and the active interpreter."""

    python = tmp_path / "python.exe"
    python.touch()
    availability = iter((False, False, True))
    monkeypatch.setattr(dependencies, "_module_available", lambda _name: next(availability))
    monkeypatch.setattr(dependencies, "_uv_executable", lambda _python: "uv")
    calls: list[list[str]] = []

    def _run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, "installed", "")

    monkeypatch.setattr(subprocess, "run", _run)

    assert dependencies.ensure_claude_code_support(python_executable=str(python)) is True
    assert calls == [
        [
            "uv",
            "pip",
            "install",
            "--python",
            str(python),
            "claude-agent-sdk>=0.2.110",
        ]
    ]
