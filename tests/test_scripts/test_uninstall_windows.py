"""uninstall.ps1's purge scopes, run under PowerShell 7 with the Windows-only cmdlets stubbed."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "install" / "uninstall.ps1"
PWSH = shutil.which("pwsh")
needs_pwsh = pytest.mark.skipif(PWSH is None, reason="PowerShell 7 (pwsh) is not installed")

# The script under test runs as the relaunched copy; its Windows-only commands are no-ops.
WRAPPER = """
function Get-NetTCPConnection { }
function Get-CimInstance { }
function taskkill { }
& $env:CLIO_TEST_UNINSTALLER -Relaunched @args
exit $LASTEXITCODE
"""
ROLES = ("cache", "config", "data", "state")


def _agent_home(tmp_path: Path, name: str = "clio-agent") -> Path:
    home = tmp_path / name
    for role in ROLES:
        (home / role).mkdir(parents=True)
        (home / role / "kept.txt").write_text("x", encoding="utf-8")
    return home


def _uninstall(tmp_path: Path, home: Path, *flags: str) -> subprocess.CompletedProcess[str]:
    assert PWSH is not None
    wrapper = tmp_path / "run.ps1"
    wrapper.write_text(WRAPPER, encoding="utf-8")
    env = {
        **os.environ,
        "HOME": str(tmp_path / "home"),
        "APPDATA": str(tmp_path / "appdata"),
        "LOCALAPPDATA": str(tmp_path / "localappdata"),
        "CLIO_BIN_DIR": str(tmp_path / "bin"),
        "CLIO_AGENT_HOME": str(home),
        "CLIO_TEST_UNINSTALLER": str(SCRIPT),
    }
    for name in ("CLIO_PREFIX", "CLIO_USER_DIR", *(f"CLIO_AGENT_{r.upper()}_DIR" for r in ROLES)):
        env.pop(name, None)
    return subprocess.run(
        [PWSH, "-NoProfile", "-NonInteractive", "-File", str(wrapper), "-Yes", *flags],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def _left(home: Path) -> set[str]:
    return {path.parent.name for path in home.rglob("kept.txt")}


@needs_pwsh
def test_without_scopes_every_root_is_kept(tmp_path: Path) -> None:
    home = _agent_home(tmp_path)
    result = _uninstall(tmp_path, home)
    assert result.returncode == 0, result.stdout + result.stderr
    assert _left(home) == set(ROLES)
    assert "pass -PurgeCaches to remove" in result.stdout
    assert "pass -PurgeData to remove" in result.stdout


@needs_pwsh
def test_each_scope_removes_only_its_roots(tmp_path: Path) -> None:
    home = _agent_home(tmp_path)
    result = _uninstall(tmp_path, home, "-PurgeCaches")
    assert result.returncode == 0, result.stdout + result.stderr
    assert _left(home) == {"config", "data", "state"}

    result = _uninstall(tmp_path, home, "-PurgeData")
    assert result.returncode == 0, result.stdout + result.stderr
    assert _left(home) == {"config"}

    result = _uninstall(tmp_path, home, "-Purge")
    assert result.returncode == 0, result.stdout + result.stderr
    assert _left(home) == set()


@needs_pwsh
def test_a_custom_root_is_refused_before_anything_is_removed(tmp_path: Path) -> None:
    home = _agent_home(tmp_path, name="custom")
    result = _uninstall(tmp_path, home, "-PurgeData")
    assert result.returncode != 0
    assert "Refusing to purge a custom data or state root" in result.stdout + result.stderr
    assert _left(home) == set(ROLES)


def test_the_relaunch_forwards_every_scope() -> None:
    """The uninstaller re-runs from a temp copy; a dropped switch would silently keep data."""
    script = SCRIPT.read_text(encoding="utf-8")
    for switch in ("Yes", "Purge", "PurgeCaches", "PurgeData"):
        assert f"[switch]${switch}" in script
        assert re.search(rf"if \(\${switch}\)\s*\{{ \$argList \+= '-{switch}' \}}", script), switch
