"""F014: a native worker settles when its server dies while a descendant holds the pipe."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from clio_agent.gact.infrastructure import node_service

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux supervisor")

# Forks an engine-like child that inherits stdout and outlives the server.
FAKE_SERVER = """import os, sys, time
pid = os.fork()
if pid == 0:
    open(sys.argv[1], "w").write(str(os.getpid()))
    time.sleep(120)
    os._exit(0)
print("api server up", flush=True)
time.sleep(0.5)
os._exit(9)
"""


def test_worker_records_a_dead_server_and_ends_its_orphaned_engine(tmp_path: Path) -> None:
    root = tmp_path / "srv" / "native" / "vllm"
    root.mkdir(parents=True)
    (root / "logs").mkdir()
    venv_bin = root / "environment/.venv/bin"
    venv_bin.mkdir(parents=True)
    (venv_bin / "python").symlink_to(sys.executable)
    (root / "launch.py").write_text(FAKE_SERVER)
    child_file = tmp_path / "child.pid"
    (root / "manifest.json").write_text(json.dumps({"arguments": [str(child_file)]}))
    node_service.write_json(root / "receipt.json", {"generation": "g1", "phase": "running"})
    script = (
        "import sys; from pathlib import Path; "
        "from clio_agent.gact.infrastructure import node_service as n; "
        "n.worker(Path(sys.argv[1]), 'start', 'g1')"
    )
    worker = subprocess.Popen([sys.executable, "-c", script, str(root)], start_new_session=True)
    try:
        assert worker.wait(timeout=60) == 0
    finally:
        if worker.poll() is None:
            os.killpg(worker.pid, 9)
    receipt = json.loads((root / "receipt.json").read_text())
    assert receipt["phase"] == "failed"
    assert receipt["pid"] == 0
    assert "start exited with code 9" in receipt["error"]
    assert "api server up" in (root / "logs/server.log").read_text()
    orphan = int(child_file.read_text())
    deadline = time.monotonic() + 5
    while node_service.identity(orphan) and time.monotonic() < deadline:
        time.sleep(0.1)
    assert not node_service.identity(orphan), "orphaned engine process survived its server"
