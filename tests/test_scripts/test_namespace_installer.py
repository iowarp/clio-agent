"""Execute installer boundaries against isolated real files and processes."""

from __future__ import annotations

import os
import shlex
import socket
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="POSIX installer acceptance")


@pytest.mark.parametrize("purge", [False, True])
def test_uninstall_uses_namespace_pid_and_preserves_data(tmp_path: Path, purge: bool) -> None:
    """The installed resolver locates the PID even when the port is nonstandard."""
    prefix = tmp_path / "program"
    python = prefix / "clio-agent/.venv/bin/python"
    python.parent.mkdir(parents=True)
    python.write_text(f'#!/bin/sh\nexec {shlex.quote(sys.executable)} "$@"\n')
    python.chmod(0o755)
    unknown = prefix / "clio-agent/research.txt"
    unknown.write_text("keep")
    agent_home = tmp_path / "agent-home"
    state = agent_home / "state"
    state.mkdir(parents=True)
    data = agent_home / "data/sessions.json"
    data.parent.mkdir()
    data.write_text("valuable")
    env = {key: value for key, value in os.environ.items() if not key.startswith("CLIO_")}
    env.update(
        HOME=str(tmp_path),
        CLIO_PREFIX=str(prefix),
        CLIO_AGENT_HOME=str(agent_home),
        CLIO_BIN_DIR=str(tmp_path / "bin"),
        CLIO_PORT="61991",
    )
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        (state / f"clio-server.{socket.gethostname().split('.')[0]}.pid").write_text(
            str(process.pid)
        )
        result = subprocess.run(
            ["bash", str(ROOT / "install/uninstall.sh"), "--yes", *(["--purge"] if purge else [])],
            env=env,
            capture_output=True,
            text=True,
            timeout=15,
        )
        if purge:
            assert result.returncode == 2, result.stdout + result.stderr
            assert "Refusing to purge a custom config root" in result.stderr
            assert process.poll() is None
            assert python.exists()
        else:
            assert result.returncode == 0, result.stdout + result.stderr
            assert process.wait(timeout=3) < 0
            assert not python.exists()
        assert data.read_text() == "valuable"
        assert unknown.read_text() == "keep"
    finally:
        if process.poll() is None:
            process.terminate()
        process.wait(timeout=3)


def test_source_reinstall_refuses_unknown_files(tmp_path: Path) -> None:
    """Source installation cannot wipe an existing directory without .clio markers."""
    prefix = tmp_path / "program"
    unknown = prefix / "clio-agent/research.txt"
    unknown.parent.mkdir(parents=True)
    unknown.write_text("keep")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    # Prerequisite discovery only; no dependency/network operation may run.
    for command in ("curl", "uv", "git", "go"):
        shim = bin_dir / command
        shim.write_text("#!/bin/sh\nexit 99\n")
        shim.chmod(0o755)
    env = dict(os.environ)
    env.update(
        CLIO_PREFIX=str(prefix),
        CLIO_REF="develop",
        CLIO_BIN_DIR=str(bin_dir),
        PATH=f"{bin_dir}:{os.environ['PATH']}",
    )
    result = subprocess.run(
        ["bash", str(ROOT / "install/install.sh")],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 1
    assert "Source reinstall refused" in result.stderr
    assert unknown.read_text() == "keep"
