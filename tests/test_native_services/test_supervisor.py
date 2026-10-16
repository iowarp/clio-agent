"""Real detached native-worker lifecycle on Windows and POSIX, using owned loopback data."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import signal
import socket
import subprocess
import sys
import time
import venv
from pathlib import Path
from typing import Any

import pytest

SOURCE = Path(__file__).resolve().parents[2] / "src/clio_agent/gact/infrastructure"


def standalone(name: str, filename: str) -> Any:
    """Load the actual shipped stdlib files without importing the application runtime."""
    spec = importlib.util.spec_from_file_location(name, SOURCE / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


platform = standalone("clio_native_platform", "native_service_platform.py")
process_group = standalone("clio_process_group", "process_group.py")
node_service = standalone("clio_native_controller", "node_service.py")

LAUNCHER = """
import json, os
from http.server import BaseHTTPRequestHandler, HTTPServer
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/v1/models' and self.headers.get('Authorization') != 'Bearer owned-test-key':
            self.send_response(401); self.end_headers(); return
        self.send_response(200); self.end_headers()
        self.wfile.write(json.dumps({'data': [{'id': 'owned-fixture'}]}).encode())
HTTPServer(('127.0.0.1', int(os.environ['OWNED_PORT'])), Handler).serve_forever()
"""


def wait_for(predicate: Any, timeout: float = 15) -> None:
    """Wait for the actual process/endpoint transition, never a fixed startup sleep."""
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            pytest.fail("The owned native service did not reach its expected state")
        time.sleep(0.05)


@pytest.fixture
def service(tmp_path: Path) -> Any:
    root = tmp_path / "native service with spaces"
    node_service.prepare(root, "test-owner")
    for name in ("logs", "evidence", "cache", "tmp"):
        (root / name).mkdir()
    venv.EnvBuilder(with_pip=False).create(root / "environment/.venv")
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    manifest = {
        "definition_version": "native-test-1",
        "port": port,
        "health_path": "/healthz",
        "identity": {"kind": "openai", "served_model": "owned-fixture"},
        "arguments": [],
        "environment": {"OWNED_PORT": str(port)},
    }
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (root / "launch.py").write_text(LAUNCHER, encoding="utf-8")
    node_service.write_json(
        root / "receipt.json",
        {
            "installed": True,
            "phase": "stopped",
            "configuration_revision": hashlib.sha256(
                json.dumps(manifest, sort_keys=True).encode()
            ).hexdigest(),
        },
    )
    request = {
        "root": str(root),
        "owner": "test-owner",
        "action": "start",
        "operation_id": "owned-start",
        "manifest": manifest,
        "api_key": "owned-test-key",
        "script": Path(node_service.__file__).read_text(encoding="utf-8"),
        "platform_helper": Path(platform.__file__).read_text(encoding="utf-8"),
        "process_group_helper": process_group.source(),
    }
    try:
        yield root, request
    finally:
        if root.exists():
            node_service.control({**request, "action": "stop"})


def test_start_status_stop_restart_uninstall_and_delete_owned_data(service: Any) -> None:
    root, request = service
    node_service.control(request)
    wait_for(lambda: node_service.observation(root, key="owned-test-key")["serving"])
    first = node_service.read_receipt(root)
    assert first["pid"] > 0 and node_service.alive(first)
    if sys.platform == "win32":
        assert first["launcher_pid"] > 0 and first["launcher_identity"]
    node_service.control(request)  # Idempotent start owns the same worker.
    assert node_service.read_receipt(root)["pid"] == first["pid"]
    with pytest.raises(ValueError, match="another deployment"):
        node_service.control({**request, "action": "stop", "owner": "foreign-owner"})
    assert node_service.alive(first)
    node_service.control({**request, "action": "stop"})
    assert not node_service.alive(first)
    if sys.platform == "win32":
        assert platform.identity(first["launcher_pid"]) != first["launcher_identity"]
    assert not node_service.listeners(request["manifest"]["port"])
    assert node_service.observation(root)["installed"]
    node_service.control(request)
    wait_for(lambda: node_service.observation(root, key="owned-test-key")["serving"])
    second = node_service.read_receipt(root)
    assert second["generation"] != first["generation"]
    node_service.control({**request, "action": "uninstall"})
    assert not node_service.alive(second)
    assert not platform.python_path(root).exists()
    assert (root / "logs/server.log").exists()  # Uninstall retains evidence.
    deleted = node_service.control({**request, "action": "delete_data"})
    assert deleted["data_deleted"] and not root.exists()


def test_reused_pid_identity_cannot_stop_a_foreign_process(service: Any) -> None:
    root, request = service
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        receipt = node_service.read_receipt(root)
        receipt.update(
            pid=child.pid,
            process_identity="foreign-creation-time",
            launcher_pid=child.pid,
            launcher_identity="foreign-creation-time",
        )
        node_service.write_json(root / "receipt.json", receipt)
        node_service.control({**request, "action": "stop"})
        assert child.poll() is None
    finally:
        child.kill()
        child.wait(timeout=5)


def test_identity_and_environment_use_the_execution_hosts_interpreter(tmp_path: Path) -> None:
    assert node_service.identity(os.getpid())
    assert node_service.identity(0) == ""
    env = node_service.worker_environment(
        tmp_path, {}, {"PATH": "original", "VIRTUAL_ENV": "foreign"}
    )
    assert "VIRTUAL_ENV" not in env
    assert env["PATH"].split(os.pathsep)[0] == str(platform.python_path(tmp_path).parent)
    assert env["TMP"] == env["TEMP"] == str(tmp_path / "tmp")


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Job Object hard-kill contract")
def test_hard_kill_of_owned_worker_reaps_its_real_server(service: Any) -> None:
    root, request = service
    node_service.control(request)
    wait_for(lambda: node_service.observation(root, key="owned-test-key")["serving"])
    receipt = node_service.read_receipt(root)
    assert node_service.alive(receipt)
    os.kill(receipt["pid"], signal.SIGTERM)  # Windows terminates this exact owned test process.
    wait_for(lambda: not node_service.alive(receipt))
    wait_for(lambda: not node_service.listeners(request["manifest"]["port"]))
    assert node_service.observation(root)["phase"] == "stopped"


def test_server_exit_reaps_descendant_holding_the_output_pipe(service: Any) -> None:
    root, request = service
    # A Windows venv may insert an interpreter redirector. Wait for the real
    # descendant to start and publish its own PID before its server exits, so
    # the test exercises an inherited output pipe rather than launcher startup.
    (root / "launch.py").write_text(
        "import subprocess,sys,time\nfrom pathlib import Path\n"
        "child=subprocess.Popen([sys.executable,'-c',"
        '"import os,time; from pathlib import Path; '
        "Path('owned-child.pid').write_text(str(os.getpid())); time.sleep(90)\"])\n"
        "Path('owned-launcher.pid').write_text(str(child.pid))\n"
        "deadline=time.monotonic()+10\n"
        "while not Path('owned-child.pid').is_file():\n"
        "    if time.monotonic() >= deadline: raise RuntimeError('Descendant never started')\n"
        "    time.sleep(0.01)\n",
        encoding="utf-8",
    )
    node_service.control(request)
    wait_for(lambda: (root / "owned-child.pid").is_file())
    child = int((root / "owned-child.pid").read_text())
    launcher = int((root / "owned-launcher.pid").read_text())
    try:
        wait_for(lambda: node_service.read_receipt(root)["phase"] == "stopped")
    except pytest.fail.Exception:
        receipt = node_service.read_receipt(root)
        logs = (root / "logs/supervisor.log").read_text(encoding="utf-8")
        pytest.fail(f"The worker did not settle after server exit: {receipt!r}; logs: {logs}")
    assert not node_service.identity(child)
    assert not node_service.identity(launcher)
    assert not node_service.observation(root)["worker_alive"]
