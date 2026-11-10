"""The POSIX launcher starts, stops and throttles on hosts without GNU tools.

macOS ships neither ``setsid(1)`` nor GNU ``stat -c``. ``clio start`` used to
``exec setsid`` the server and so never started on a Mac, and the update check's
stamp age was always "now", so it hit PyPI on every invocation.
"""

from __future__ import annotations

import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

LAUNCHER = Path(__file__).resolve().parents[2] / "install" / "clio"

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX launcher")

_TOOLS = (
    "bash",
    "cat",
    "curl",
    "date",
    "env",
    "grep",
    "head",
    "hostname",
    "lsof",
    "mkdir",
    "rm",
    "seq",
    "sleep",
    "stat",
    "touch",
    "uname",
)

_FAKE_SERVER = """\
import http.server, os, subprocess, sys
port = int(sys.argv[sys.argv.index("--port") + 1])
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
with open(os.environ["FAKE_CHILD_PIDFILE"], "w") as fh:
    fh.write(str(child.pid))
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers(); self.wfile.write(b"ok")
    def log_message(self, *a):
        pass
http.server.HTTPServer(("127.0.0.1", port), H).serve_forever()
"""


def _tools_without_setsid(root: Path) -> Path:
    tools = root / "tools"
    tools.mkdir()
    for name in _TOOLS:
        found = shutil.which(name)
        if found:
            (tools / name).symlink_to(found)
    assert not (tools / "setsid").exists()
    return tools


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _install(root: Path) -> Path:
    prefix = root / "prefix"
    venv_bin = prefix / "clio-agent" / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    state = root / "state"
    python = venv_bin / "python"
    python.write_text(
        "#!/usr/bin/env bash\n"
        f'case "$*" in *clio_agent.paths*) echo "{state}";;'
        f' *) exec "{sys.executable}" "$@";; esac\n'
    )
    python.chmod(0o755)
    server = venv_bin / "clio-agent"
    server.write_text(f"#!{sys.executable}\n{_FAKE_SERVER}")
    server.chmod(0o755)
    return prefix


def _run(args: list[str], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(LAUNCHER), *args],
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
        check=False,
    )


@pytest.mark.timeout(150)  # Starting and group-stopping a real fake server takes a few seconds.
def test_start_without_setsid_leads_a_group_and_stop_kills_its_children(tmp_path: Path) -> None:
    tools = _tools_without_setsid(tmp_path)
    if not (tools / "curl").exists():
        pytest.skip("curl is required by the launcher health check")
    prefix = _install(tmp_path)
    child_pidfile = tmp_path / "child.pid"
    env = {
        "PATH": str(tools),
        "HOME": str(tmp_path / "home"),
        "CLIO_PREFIX": str(prefix),
        "CLIO_BIN_DIR": str(tmp_path / "bin"),
        "CLIO_PORT": str(_free_port()),
        "CLIO_NO_UPDATE_CHECK": "1",
        "FAKE_CHILD_PIDFILE": str(child_pidfile),
    }
    started = _run(["start"], env)
    server_pid = 0
    child_pid = 0
    try:
        assert started.returncode == 0, started.stdout + started.stderr
        assert "healthy" in started.stdout
        pidfile = next((tmp_path / "state").glob("clio-server.*.pid"))
        server_pid = int(pidfile.read_text().strip())
        assert os.getpgid(server_pid) == server_pid
        for _ in range(100):
            if child_pidfile.exists() and child_pidfile.read_text():
                break
            time.sleep(0.05)
        child_pid = int(child_pidfile.read_text())
        assert os.getpgid(child_pid) == server_pid

        stopped = _run(["stop"], env)
        assert stopped.returncode == 0, stopped.stdout + stopped.stderr
        for _ in range(100):
            if not _alive(child_pid):
                break
            time.sleep(0.05)
        assert not _alive(child_pid)
    finally:
        for pid in (server_pid, child_pid):
            if pid and _alive(pid):
                os.kill(pid, signal.SIGKILL)


def test_update_check_honors_a_fresh_stamp_with_bsd_or_gnu_stat(tmp_path: Path) -> None:
    tools = _tools_without_setsid(tmp_path)
    marker = tmp_path / "curl-called"
    (tools / "curl").unlink(missing_ok=True)
    (tools / "curl").write_text(f"#!/bin/sh\ntouch {marker}\n")
    (tools / "curl").chmod(0o755)
    stamp = tmp_path / "stamp"
    stamp.touch()
    text = LAUNCHER.read_text(encoding="utf-8")
    start = text.index("file_mtime() {")
    end = text.index("\n}\n", text.index("update_check() {")) + 3
    script = (
        "set -euo pipefail\n"
        'warn() { echo "WARN $*"; }\n'
        f'CLIO_VENV="{tmp_path / "venv"}"\n'
        f'UPDATE_STAMP="{stamp}"\n'
        f"{text[start:end]}"
        "update_check\n"
        "echo COMMAND_RAN\n"
    )
    result = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        timeout=60,
        env={"PATH": str(tools)},
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "COMMAND_RAN" in result.stdout
    assert not marker.exists(), "a fresh stamp must skip the PyPI request"
