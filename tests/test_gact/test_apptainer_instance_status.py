"""F034: an Apptainer instance whose server exited is not reported running."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from clio_agent.gact.infrastructure.container_runtime import status_command

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="Apptainer is Linux-only")


def _status(tmp_path: Path, *, listed: bool, children: bool) -> str:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    row = 'echo "clio-llama-cpp    4242          /x.sif"' if listed else ":"
    (bin_dir / "apptainer").write_text(
        f'#!/bin/sh\necho "INSTANCE NAME    PID    IP    IMAGE"\n{row}\n'
    )
    (bin_dir / "pgrep").write_text(
        f'#!/bin/sh\n[ "$1 $2" = "-P 4242" ] && exit {0 if children else 1}\nexit 2\n'
    )
    for tool in bin_dir.iterdir():
        tool.chmod(0o755)
    spec = status_command("apptainer", "clio-llama-cpp")
    env = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}
    result = subprocess.run(
        [spec.program, *spec.args], capture_output=True, text=True, env=env, check=True
    )
    return result.stdout.strip()


def test_an_instance_with_its_server_is_running(tmp_path: Path) -> None:
    assert _status(tmp_path, listed=True, children=True) == "running"


def test_an_instance_whose_server_exited_reports_exited(tmp_path: Path) -> None:
    assert _status(tmp_path, listed=True, children=False) == "exited"


def test_no_instance_is_stopped(tmp_path: Path) -> None:
    assert _status(tmp_path, listed=False, children=False) == "stopped"
