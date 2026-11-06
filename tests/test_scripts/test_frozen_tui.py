"""Current launchers must never execute an obsolete TUI left by an upgrade."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from scripts.check_release_completeness import find_missing
from tests.test_scripts.installer_shims import shell_path
from tests.test_scripts.test_check_release_completeness import _COMPLETE_ASSETS

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("shell", ["bash", "powershell"] if os.name == "nt" else ["bash"])
@pytest.mark.parametrize("command", ["attach", "tui", "--tui", ""])
def test_frozen_tui_is_never_executed(tmp_path: Path, shell: str, command: str) -> None:
    """Default launch uses Web; explicit legacy entry points fail before startup."""
    prefix = tmp_path / "prefix"
    prefix.mkdir()
    marker = tmp_path / "tui-ran"
    legacy = prefix / "gact"
    legacy.write_text(f'#!/usr/bin/env bash\ntouch "{marker.as_posix()}"\n', encoding="utf-8")
    legacy.chmod(0o755)
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("CLIO_", "GACT_", "XDG_"))
    }
    env.update(
        HOME=str(tmp_path / "home"),
        USERPROFILE=str(tmp_path / "home"),
        LOCALAPPDATA=str(tmp_path / "local"),
        APPDATA=str(tmp_path / "roaming"),
        CLIO_AGENT_HOME=str(tmp_path / "agent"),
        CLIO_PREFIX=str(prefix),
        CLIO_BIN_DIR=str(tmp_path / "bin"),
        CLIO_NO_UPDATE_CHECK="1",
    )
    if shell == "bash":
        for name in ("HOME", "CLIO_AGENT_HOME", "CLIO_PREFIX", "CLIO_BIN_DIR"):
            env[name] = shell_path(Path(env[name]))
        bash = "C:/Program Files/Git/bin/bash.exe" if os.name == "nt" else shutil.which("bash")
        assert bash is not None
        args = [bash, str(ROOT / "install/clio")]
    else:
        args = ["powershell.exe", "-NoProfile", "-File", str(ROOT / "install/clio.ps1")]
    if command:
        args.append(command)
    result = subprocess.run(args, env=env, capture_output=True, text=True, timeout=20, check=False)
    output = result.stdout + result.stderr
    assert not marker.exists(), output
    if command:
        assert result.returncode == 2, output
        assert "TUI is frozen and unsupported" in output
    else:
        assert result.returncode == 1, output
        assert "web bundle not found" in output


def test_release_is_complete_without_any_tui_payload() -> None:
    """Desktop, Web, updater and installer requirements remain enforced."""
    assets = [name for name in _COMPLETE_ASSETS if not name.startswith("clio-tui-")]
    assert not find_missing(assets)
    assert find_missing([name for name in assets if name != "clio-web-0.5.17.zip"])
