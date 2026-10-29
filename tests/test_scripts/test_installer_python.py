"""Each install takes the Python its ref declares: `.python-version`, or 3.13 before it."""

from __future__ import annotations

from pathlib import Path

from tests.test_scripts.installer_shims import (
    RELEASE_STOP,
    ROOT,
    run_installer,
    write_release,
)


def _venv_python(calls: list[str]) -> str:
    venv = next(call for call in calls if call.startswith("uv venv"))
    return venv.split("--python ", 1)[1].split()[0]


def test_a_release_without_python_version_keeps_the_core_compatible_3_13(tmp_path: Path) -> None:
    """Older published releases pin clio-core 2.2.x, whose wheels need Python 3.13."""
    write_release(tmp_path / "fixtures", "v0.9.4.24", python=None)
    result, calls = run_installer(tmp_path, "--version", "0.9.4.24")
    assert result.returncode == RELEASE_STOP, result.stdout + result.stderr
    assert "could not install clio-agent" in result.stderr
    assert _venv_python(calls) == "3.13"


def test_a_release_with_python_version_uses_it(tmp_path: Path) -> None:
    write_release(tmp_path / "fixtures", "v0.9.5-beta.8", python="3.14")
    result, calls = run_installer(tmp_path, "--version", "0.9.5b8")
    assert result.returncode == RELEASE_STOP, result.stdout + result.stderr
    assert "could not install clio-agent" in result.stderr
    assert _venv_python(calls) == "3.14"


def test_both_installers_read_the_python_from_the_ref() -> None:
    shell = (ROOT / "install/install.sh").read_text()
    powershell = (ROOT / "install/install.ps1").read_text()
    assert "printf '%s' \"${value:-3.13}\"" in shell
    assert 'uv venv --python "$py"' in shell
    assert "return '3.13'" in powershell
    assert "& uv venv --python $py $Venv" in powershell
    assert "$ok = $LASTEXITCODE -eq 0" in powershell
    for script in (shell, powershell):
        assert "pip release installation" not in script  # one installer: uv
        assert ">=3.12" not in script
        assert ">=3.13" not in script
        assert ">=3.14" not in script
