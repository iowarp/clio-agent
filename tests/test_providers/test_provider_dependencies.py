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
    """Claude repair installs the newest release with a wheel HERE, into the active interpreter."""

    python = tmp_path / "python.exe"
    python.touch()
    monkeypatch.setattr(
        dependencies, "claude_code_requirement", lambda: "claude-agent-sdk==0.2.159"
    )
    availability = iter((False, False, False, True))
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
            "claude-agent-sdk==0.2.159",
        ]
    ]


def test_claude_code_install_forgets_a_selection_cached_before_it(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """A ``claude_no_cli`` selection cached before Install must not outlive it.

    Desktop regression: the provider panel's refresh cached ``claude_no_cli``,
    Install succeeded, and every ``POST .../claude_code/auth`` then failed 502
    ("Claude Code runtime is unavailable") until the backend restarted.
    """
    from clio_agent.providers.components import client_binary

    python = tmp_path / "python"
    python.touch()
    monkeypatch.setattr(
        dependencies, "claude_code_requirement", lambda: "claude-agent-sdk==0.2.179"
    )
    availability = iter((False, False, False, True))
    monkeypatch.setattr(dependencies, "_module_available", lambda _name: next(availability))
    monkeypatch.setattr(dependencies, "_uv_executable", lambda _python: "uv")
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda command, **_kwargs: subprocess.CompletedProcess(command, 0, "", ""),
    )
    stale = client_binary.ClientSelection(None, "claude_no_cli")
    bundled = client_binary.ClientBinary(
        path=str(tmp_path / "claude"), version="2.1.0", source="bundled"
    )
    selections = iter((stale, client_binary.ClientSelection(bundled, "claude_installed_not_found")))
    monkeypatch.setattr(client_binary, "_select_claude", lambda: next(selections))
    client_binary.reset_client_cache()
    assert client_binary.claude_client().reason == "claude_no_cli"

    assert dependencies.ensure_claude_code_support(python_executable=str(python)) is True

    assert client_binary.claude_client().client == bundled
    client_binary.reset_client_cache()


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

    def test_installs_the_metadata_resolved_spec_with_a_fake_runner(self, monkeypatch: Any) -> None:
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

        with pytest.raises(dependencies.ProviderExtraNotInstallableError, match="not-a-real-extra"):
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


class TestSealedRuntimeOverlay:
    """A sealed desktop runtime (a signed macOS app) never installs into itself:
    provider support goes to the user-writable package overlay."""

    def test_installs_into_the_overlay_pinned_to_the_runtime(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        from clio_agent.runtime import package_overlay  # noqa: PLC0415

        overlay = tmp_path / "data" / "runtime-packages" / "key"
        monkeypatch.setattr(package_overlay, "overlay_for", lambda _python: overlay)
        monkeypatch.setattr(package_overlay, "prepend", lambda _directory: None)
        availability = iter((False, False, True))
        monkeypatch.setattr(dependencies, "_module_available", lambda _name: next(availability))
        monkeypatch.setattr(dependencies, "_uv_executable", lambda _python: "uv")
        seen: dict[str, Any] = {}

        def _fake_runner(command: list[str]) -> subprocess.CompletedProcess[str]:
            seen["command"] = command
            constraints = Path(command[command.index("--constraint") + 1])
            seen["pins"] = constraints.read_text(encoding="utf-8").split()
            target = Path(command[command.index("--target") + 1])
            seen["overlay_during_install"] = sorted(p.name for p in overlay.iterdir())
            (target / "globus_sdk").mkdir()
            (target / "globus_sdk" / "__init__.py").write_text("", encoding="utf-8")
            info = target / "globus_sdk-3.0.0.dist-info"
            info.mkdir()
            (info / "METADATA").write_text(
                "Metadata-Version: 2.1\nName: globus-sdk\nVersion: 3.0.0\n", encoding="utf-8"
            )
            return subprocess.CompletedProcess(command, 0, "installed", "")

        monkeypatch.setattr(dependencies, "_run_install_command", _fake_runner)

        assert dependencies.ensure_provider_extra(
            extra_name="argonne",
            module_name="globus_sdk",
            display_name="ALCF support",
            python_executable="python",
        )
        command = seen["command"]
        assert command[:5] == ["uv", "pip", "install", "--python", "python"]
        # The installer writes beside the live overlay; a concurrent import (migrate during
        # an install) never sees a half-written dependency such as pydantic.
        staging = Path(command[command.index("--target") + 1])
        assert staging.parent == overlay.parent
        assert staging.name.startswith(".staging-key-")
        assert seen["overlay_during_install"] == []
        assert not staging.exists()
        assert (overlay / "globus_sdk" / "__init__.py").is_file()
        assert (overlay / "globus_sdk-3.0.0.dist-info" / "METADATA").is_file()
        # A sealed runtime's support packages use CLIO's own cache (purged on uninstall),
        # never the person's ~/.cache/uv (live: 336 MB of claude-agent-sdk left behind).
        from clio_agent import paths  # noqa: PLC0415

        cache = Path(command[command.index("--cache-dir") + 1])
        assert cache.is_relative_to(paths.user_cache_dir())
        assert "--compile-bytecode" in command
        assert command[-1] == "globus-sdk>=3.0.0"
        assert overlay.is_dir()
        import importlib.metadata as metadata  # noqa: PLC0415

        assert f"pytest=={metadata.version('pytest')}" in seen["pins"]
        assert not any(pin.startswith("globus-sdk==") for pin in seen["pins"])

    @staticmethod
    def _dist(root: Path, name: str, module: str, version: str) -> None:
        (root / module).mkdir(parents=True)
        (root / module / "__init__.py").write_text("", encoding="utf-8")
        info = root / f"{module}-{version}.dist-info"
        info.mkdir()
        (info / "METADATA").write_text(
            f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n", encoding="utf-8"
        )
        (info / "RECORD").write_text(
            f"{module}/__init__.py,,\n{info.name}/METADATA,,\n{info.name}/RECORD,,\n",
            encoding="utf-8",
        )

    def _overlay_install(
        self, tmp_path: Path, monkeypatch: Any, results: dict[str, int | str]
    ) -> tuple[Path, list[list[str]]]:
        """Run an overlay install whose fake uv answers each step from ``results``.

        The resolve names globus-sdk (lacking) and pytest at the runtime's version,
        which the overlay holds a stale copy of.
        """
        import importlib.metadata as metadata  # noqa: PLC0415

        from clio_agent.runtime import package_overlay  # noqa: PLC0415

        overlay = tmp_path / "data" / "runtime-packages" / "key"
        self._dist(overlay, "pytest", "pytest_stale", "0.0.1")
        monkeypatch.setattr(package_overlay, "overlay_for", lambda _python: overlay)
        monkeypatch.setattr(package_overlay, "prepend", lambda _directory: None)
        availability = iter((False, False, True))
        monkeypatch.setattr(dependencies, "_module_available", lambda _name: next(availability))
        monkeypatch.setattr(dependencies, "_uv_executable", lambda _python: "uv")
        pytest_pin = f"pytest=={metadata.version('pytest')}"
        commands: list[list[str]] = []

        def _fake_runner(command: list[str]) -> subprocess.CompletedProcess[str]:
            commands.append(command)
            step = "dry" if "--dry-run" in command else "gaps" if "--no-deps" in command else "full"
            outcome = results.get(step, 0)
            if step == "dry":
                if isinstance(outcome, int) and outcome:
                    return subprocess.CompletedProcess(command, outcome, "", "no solution")
                listing = (
                    outcome
                    if isinstance(outcome, str)
                    else f"Would install 2 packages\n + globus-sdk==3.0.0\n + {pytest_pin}\n"
                )
                return subprocess.CompletedProcess(command, 0, "", listing)
            if outcome:
                return subprocess.CompletedProcess(command, int(outcome), "", "boom")
            target = Path(command[command.index("--target") + 1])
            self._dist(target, "globus-sdk", "globus_sdk", "3.0.0")
            if step == "full":
                self._dist(target, "pytest", "pytest", metadata.version("pytest"))
            return subprocess.CompletedProcess(command, 0, "installed", "")

        monkeypatch.setattr(dependencies, "_run_install_command", _fake_runner)
        assert dependencies.ensure_provider_extra(
            extra_name="argonne",
            module_name="globus_sdk",
            display_name="ALCF support",
            python_executable="python",
        )
        # Either way the published overlay is the same: the lacking distribution, and the
        # stale copy of a runtime distribution superseded by the runtime's own.
        assert sorted(p.name for p in overlay.iterdir()) == [
            "globus_sdk",
            "globus_sdk-3.0.0.dist-info",
        ]
        assert not [p for p in overlay.parent.iterdir() if p.name.startswith(".staging-")]
        return overlay, commands

    def test_installs_only_what_the_runtime_lacks_without_its_dependencies(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        _overlay, commands = self._overlay_install(tmp_path, monkeypatch, {})
        assert len(commands) == 2
        dry_run, gaps = commands
        assert "--dry-run" in dry_run and dry_run[-1] == "globus-sdk>=3.0.0"
        assert "--no-deps" in gaps and "--compile-bytecode" in gaps
        assert gaps[-1] == "globus-sdk==3.0.0"

    @pytest.mark.parametrize(
        "results",
        [
            {"dry": 2},
            {"dry": "Resolved 2 packages\n + globus-sdk @ file:///wheel.whl\n"},
            {"dry": "Audited 1 package\n"},
            {"gaps": 1},
        ],
        ids=["resolve-fails", "unpinned-resolve", "nothing-resolved", "gap-install-fails"],
    )
    def test_falls_back_to_the_full_install(
        self, tmp_path: Path, monkeypatch: Any, results: dict[str, int | str]
    ) -> None:
        _overlay, commands = self._overlay_install(tmp_path, monkeypatch, results)
        full = commands[-1]
        assert "--dry-run" not in full and "--no-deps" not in full
        assert full[-1] == "globus-sdk>=3.0.0"
        assert len(commands) == (3 if "gaps" in results else 2)

    def test_pip_fallback_also_targets_the_overlay(self, tmp_path: Path, monkeypatch: Any) -> None:
        monkeypatch.setattr(dependencies, "_uv_executable", lambda _python: None)
        overlay = tmp_path / "overlay"
        constraints = tmp_path / "c.txt"
        command = dependencies._install_command(
            "python", ["x>=1"], target=overlay, constraints=constraints
        )
        from clio_agent.runtime import package_overlay  # noqa: PLC0415

        assert command == [
            "python",
            "-m",
            "pip",
            "install",
            "--target",
            str(overlay),
            "--cache-dir",
            str(package_overlay.overlay_install_cache()),
            "--constraint",
            str(constraints),
            "--upgrade",
            "x>=1",
        ]

    def test_the_copilot_runtime_download_child_sees_the_overlay(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        from clio_agent.providers import support_record  # noqa: PLC0415
        from clio_agent.providers.components import client_binary  # noqa: PLC0415
        from clio_agent.runtime import package_overlay  # noqa: PLC0415

        overlay = tmp_path / "overlay"
        monkeypatch.setattr(package_overlay, "overlay_for", lambda _python: overlay)
        monkeypatch.setattr(dependencies, "ensure_provider_extra", lambda **_kwargs: True)
        monkeypatch.setattr(support_record, "record_support", lambda _kind: None)
        monkeypatch.setattr(client_binary, "reset_client_cache", lambda: None)
        calls: list[list[str]] = []

        def _fake_runner(command: list[str]) -> subprocess.CompletedProcess[str]:
            calls.append(command)
            return subprocess.CompletedProcess(command, 0, "", "")

        monkeypatch.setattr(dependencies, "_run_install_command", _fake_runner)
        assert dependencies.ensure_copilot_support(python_executable="python") is True
        (command,) = calls
        assert command[:2] == ["python", "-c"]
        assert repr(str(overlay)) in command[2] and "'copilot'" in command[2]
        assert command[3:] == ["download-runtime"]
