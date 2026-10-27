"""The POSIX launcher's update check never stops the command it runs before.

The check runs first in ``clio start`` under ``set -euo pipefail``. An install newer
than PyPI's latest stable (any beta) used to end the check on a false test, so the
first ``clio start`` after a fresh install exited 1 without a word; that broke every
first start of a Desktop SSH deployment of a beta.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

LAUNCHER = Path(__file__).resolve().parents[2] / "install" / "clio"


def _update_check_source() -> str:
    text = LAUNCHER.read_text(encoding="utf-8")
    match = re.search(r"^update_check\(\) \{\n.*?^\}\n", text, re.MULTILINE | re.DOTALL)
    assert match, "update_check() not found in install/clio"
    return match.group(0)


def _run(tmp_path: Path, installed: str, latest: str) -> subprocess.CompletedProcess[str]:
    venv_bin = tmp_path / "venv" / "bin"
    venv_bin.mkdir(parents=True)
    fake_python = venv_bin / "python"
    fake_python.write_text(
        "#!/usr/bin/env bash\n"
        'case "$*" in *importlib.metadata*) echo "$FAKE_INSTALLED";;'
        f' *) exec {sys.executable} "$@";; esac\n'
    )
    fake_python.chmod(0o755)
    tools = tmp_path / "tools"
    tools.mkdir()
    curl = tools / "curl"
    curl.write_text(f"#!/usr/bin/env bash\necho '{json.dumps({'info': {'version': latest}})}'\n")
    curl.chmod(0o755)
    script = (
        "set -euo pipefail\n"
        'warn() { echo "WARN $*"; }\n'
        f'CLIO_VENV="{tmp_path / "venv"}"\n'
        f'UPDATE_STAMP="{tmp_path / "stamp"}"\n'
        f"{_update_check_source()}"
        "update_check\n"
        "echo COMMAND_RAN\n"
    )
    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        timeout=60,
        env={"PATH": f"{tools}:/usr/bin:/bin", "FAKE_INSTALLED": installed},
        check=False,
    )


@pytest.mark.parametrize(
    ("installed", "latest", "warns"),
    [
        ("0.9.5b8", "0.9.4.24", False),  # a beta newer than the latest stable
        ("0.9.4.24", "0.9.4.24", False),
        ("0.9.4.23", "0.9.4.24", True),
        ("0.9.5b8", "0.9.5", True),  # a beta behind its final release
    ],
)
def test_update_check_warns_only_when_behind_and_never_stops_the_command(
    tmp_path: Path, installed: str, latest: str, warns: bool
) -> None:
    result = _run(tmp_path, installed, latest)
    assert result.returncode == 0, result.stderr
    assert "COMMAND_RAN" in result.stdout
    assert ("WARN clio-agent update available" in result.stdout) is warns
