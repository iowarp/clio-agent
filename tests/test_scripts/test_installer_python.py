"""Exercise interpreter selection before an installer performs dependency downloads."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _shell_path(path: Path) -> str:
    """Return a path understood by the platform's Bash interpreter."""
    value = path.as_posix()
    return f"/{value[0].lower()}{value[2:]}" if os.name == "nt" else value


def test_posix_installer_selects_core_compatible_python(tmp_path: Path) -> None:
    """The real release branch requests 3.13 even when newer Python is available."""
    bash = "C:/Program Files/Git/bin/bash.exe" if os.name == "nt" else shutil.which("bash")
    assert bash is not None
    commands = tmp_path / "commands"
    commands.mkdir()
    invocation = tmp_path / "uv-arguments"
    uv = commands / "uv"
    uv.write_text('#!/usr/bin/env bash\nprintf "%s\\n" "$@" > "$TEST_UV_LOG"\nexit 88\n')
    uv.chmod(0o755)
    # Exercise the Linux installer under Git Bash on Windows as well.
    uname = commands / "uname"
    uname.write_text(
        '#!/usr/bin/env bash\nif [ "$1" = -s ]; then echo Linux; else echo x86_64; fi\n'
    )
    uname.chmod(0o755)
    curl = commands / "curl"
    curl.write_text("#!/usr/bin/env bash\nexit 99\n")
    curl.chmod(0o755)
    env = {key: value for key, value in os.environ.items() if not key.startswith("CLIO_")}
    env.update(
        TEST_UV_LOG=_shell_path(invocation),
        TEST_INSTALL_PATH=f"{_shell_path(commands)}:/usr/bin:/bin",
        CLIO_PREFIX=_shell_path(tmp_path / "installation"),
        CLIO_BIN_DIR=_shell_path(tmp_path / "launchers"),
        CLIO_VERSION="0.9.5b2",
    )
    result = subprocess.run(
        [
            bash,
            "-c",
            'export PATH="$TEST_INSTALL_PATH"; exec bash "$1"',
            "installer-test",
            _shell_path(ROOT / "install/install.sh"),
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 88, result.stdout + result.stderr
    assert invocation.read_text().splitlines()[:3] == ["venv", "--python", "3.13"]


def test_windows_installer_uses_same_supported_interpreter() -> None:
    """Both installers choose the bundle's minor and reject unsupported pip hosts."""
    shell = (ROOT / "install/install.sh").read_text()
    powershell = (ROOT / "install/install.ps1").read_text()
    assert "RunNative uv @('venv', '--python', '3.13', $Venv)" in powershell
    for script in (shell, powershell):
        assert "sys.version_info[:2] == (3, 13)" in script
        assert "pip installation requires Python 3.13" in script
        assert ">=3.12" not in script
        assert ">=3.13" not in script
