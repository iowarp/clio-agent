"""CLIO launches the launchers it ships from its own environment, never from PATH."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from clio_agent.runtime import installation_tools, managed_launchers


def _script(folder: Path, name: str) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / (f"{name}.exe" if os.name == "nt" else name)
    path.write_text("", encoding="utf-8")
    path.chmod(0o755)
    return path


@pytest.fixture
def environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    scripts = tmp_path / "env" / "scripts"
    monkeypatch.setattr(managed_launchers, "_declared_scripts", lambda: frozenset({"kitx"}))
    monkeypatch.setattr(managed_launchers, "_environment_script_dirs", lambda: [scripts])
    monkeypatch.setattr(installation_tools, "installation_prefix", lambda prefix=None: None)
    return scripts


def test_a_shipped_launcher_is_clios_own_even_when_path_has_another(
    environment: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    own = _script(environment, "kitx")
    ambient = _script(tmp_path / "ambient", "kitx")
    monkeypatch.setenv("PATH", str(ambient.parent))

    assert managed_launchers.resolve_launcher("kitx") == str(own)


def test_a_shipped_launcher_missing_from_clio_is_not_taken_from_path(
    environment: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ambient = _script(tmp_path / "ambient", "kitx")
    monkeypatch.setenv("PATH", str(ambient.parent))

    assert managed_launchers.resolve_launcher("kitx") is None


def test_a_launcher_clio_does_not_ship_is_found_on_path(
    environment: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ambient = _script(tmp_path / "ambient", "otherx")
    monkeypatch.setenv("PATH", str(ambient.parent))

    found = managed_launchers.resolve_launcher("otherx")

    assert found is not None
    assert Path(found).parent == ambient.parent


def test_a_scoped_installations_own_tool_folder_comes_first(
    environment: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _script(environment, "kitx")
    prefix = tmp_path / "prefix"
    installed = _script(prefix / "uv-tools" / "bin", "kitx")
    monkeypatch.setattr(installation_tools, "installation_prefix", lambda prefix_=None: prefix)

    assert managed_launchers.resolve_launcher("kitx") == str(installed)
