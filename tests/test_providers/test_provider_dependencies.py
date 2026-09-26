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
            "claude-agent-sdk==0.2.156",
        ]
    ]


class TestResolveExtraRequirements:
    """The package spec a provider install can ever run comes from CLIO's own
    declared extras (``pyproject.toml``), read fresh off installed metadata --
    never a hardcoded literal, never request input."""

    def test_reads_the_argonne_extra_from_clios_own_installed_metadata(self) -> None:
        assert dependencies._resolve_extra_requirements("argonne") == ["globus-sdk>=3.0.0"]

    def test_an_extra_clio_never_declared_resolves_to_nothing(self) -> None:
        assert dependencies._resolve_extra_requirements("not-a-real-extra") == []


@pytest.mark.real_dependency_installer
class TestEnsureProviderExtra:
    """``ensure_provider_extra`` resolves the spec from metadata and only then
    hands it to a fake installer runner -- nothing real ever executes."""

    def test_installs_the_metadata_resolved_spec_with_a_fake_runner(
        self, monkeypatch: Any
    ) -> None:
        availability = iter((False, False, True))
        monkeypatch.setattr(dependencies, "_module_available", lambda _name: next(availability))
        monkeypatch.setattr(dependencies, "_uv_executable", lambda _python: None)
        calls: list[list[str]] = []

        def _fake_runner(command: list[str]) -> subprocess.CompletedProcess[str]:
            calls.append(command)
            return subprocess.CompletedProcess(command, 0, "installed", "")

        monkeypatch.setattr(dependencies, "_run_install_command", _fake_runner)

        installed = dependencies.ensure_provider_extra(
            extra_name="argonne",
            module_name="globus_sdk",
            display_name="ALCF support",
            python_executable="python",
        )

        assert installed is True
        assert calls == [["python", "-m", "pip", "install", "globus-sdk>=3.0.0"]]

    def test_refuses_an_extra_clio_never_declared_without_running_anything(
        self, monkeypatch: Any
    ) -> None:
        monkeypatch.setattr(dependencies, "_module_available", lambda _name: False)
        run = Mock()
        monkeypatch.setattr(dependencies, "_run_install_command", run)

        with pytest.raises(
            dependencies.ProviderExtraNotInstallableError, match="not-a-real-extra"
        ):
            dependencies.ensure_provider_extra(
                extra_name="not-a-real-extra",
                module_name="whatever",
                display_name="Whatever support",
            )
        run.assert_not_called()

    def test_the_fake_runners_failure_surfaces_as_a_typed_error(self, monkeypatch: Any) -> None:
        monkeypatch.setattr(dependencies, "_module_available", lambda _name: False)
        monkeypatch.setattr(dependencies, "_uv_executable", lambda _python: None)
        monkeypatch.setattr(
            dependencies,
            "_run_install_command",
            lambda command: subprocess.CompletedProcess(command, 1, "", "no network"),
        )

        with pytest.raises(dependencies.ProviderDependencyInstallError, match="no network"):
            dependencies.ensure_provider_extra(
                extra_name="argonne",
                module_name="globus_sdk",
                display_name="ALCF support",
                python_executable="python",
            )


class TestEnsureProviderSupport:
    """The route's ONE generic entry point: dispatch by provider kind through
    the registry, never a per-provider if/else at the route layer."""

    def test_dispatches_argonne_through_the_registry(self, monkeypatch: Any) -> None:
        called: dict[str, Any] = {}

        def _fake_installer(*, python_executable: str | None = None) -> bool:
            called["argonne"] = python_executable
            return True

        monkeypatch.setitem(dependencies._PROVIDER_INSTALLERS, "argonne", _fake_installer)

        assert dependencies.ensure_provider_support("argonne", python_executable="py") is True
        assert called == {"argonne": "py"}

    def test_refuses_a_provider_kind_with_no_registered_installer(self) -> None:
        with pytest.raises(dependencies.ProviderExtraNotInstallableError, match="openai"):
            dependencies.ensure_provider_support("openai")
