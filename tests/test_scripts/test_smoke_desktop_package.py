"""Package qualification cannot accept a fallback backend or leak its auth token."""

from __future__ import annotations

import io
import json
import subprocess
from pathlib import Path
from unittest.mock import Mock

import psutil
import pytest

from scripts import smoke_desktop_package as smoke


def backend(tmp_path: Path) -> Mock:
    """Describe an owned test descendant with a real on-disk runtime manifest."""
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    python = runtime / "python"
    python.write_bytes(b"interpreter fixture")
    (runtime / "runtime.json").write_text(json.dumps({"schema": 1, "exec": ["python"]}))
    process = Mock(spec=psutil.Process)
    process.pid = 123
    process.cmdline.return_value = [str(python), "-m", "clio_agent.gact", "--port", "12345"]
    process.environ.return_value = {
        "CLIO_AUTH_TOKEN": "private-test-token",
        "GACT_BUNDLED_RUNTIME_DIR": str(runtime),
    }
    process.exe.return_value = str(python)
    return process


def test_backend_requires_the_module_port_and_token(tmp_path: Path) -> None:
    process = backend(tmp_path)
    assert smoke.backend_connection(process) == ("http://127.0.0.1:12345", "private-test-token")
    process.environ.return_value = {}
    assert smoke.backend_connection(process) is None
    process.cmdline.return_value = ["python", "-m", "other_server", "--port", "12345"]
    assert smoke.backend_connection(process) is None


def test_runtime_cannot_be_replaced_with_a_development_interpreter(tmp_path: Path) -> None:
    process = backend(tmp_path)
    assert smoke.require_bundled_python(process) == str((tmp_path / "runtime/python").resolve())
    other = tmp_path / "developer-python"
    other.write_bytes(b"other")
    process.exe.return_value = str(other)
    with pytest.raises(ValueError, match="outside its packaged runtime"):
        smoke.require_bundled_python(process)
    (tmp_path / "runtime/runtime.json").write_text(json.dumps({"exec": [str(other)]}))
    with pytest.raises(ValueError, match="outside its packaged runtime"):
        smoke.require_bundled_python(process)


@pytest.mark.parametrize("crash", [False, True])
def test_smoke_requires_survival_and_always_cleans_up_and_redacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, crash: bool
) -> None:
    candidate = backend(tmp_path)
    desktop = tmp_path / "desktop.exe"
    desktop.write_bytes(b"desktop fixture")
    native = Mock()
    native.pid = 321
    native.poll.return_value = None
    if crash:
        native.wait.return_value = 12
    else:
        native.wait.side_effect = subprocess.TimeoutExpired("desktop", 10)
    process = Mock(spec=psutil.Process)
    process.children.return_value = [candidate]
    monkeypatch.setattr(smoke.psutil, "Process", lambda pid: process)
    cleanup = Mock()
    monkeypatch.setattr(smoke, "stop_owned", cleanup)

    def launch(*args: object, **kwargs: object) -> Mock:
        log = kwargs["stdout"]
        log.write(b"private-test-token\n")  # type: ignore[union-attr]
        return native

    monkeypatch.setattr(smoke.subprocess, "Popen", launch)
    requests: list[object] = []

    def response(request: object, **kwargs: object) -> io.BytesIO:
        requests.append(request)
        return io.BytesIO(b'{"contract_version":"1"}')

    monkeypatch.setattr(smoke, "urlopen", response)
    evidence = tmp_path / "evidence"
    if crash:
        with pytest.raises(RuntimeError, match="after backend readiness"):
            smoke.smoke(desktop, evidence)
    else:
        assert smoke.smoke(desktop, evidence)["status"] == "passed"
    assert requests[0].get_header("Authorization") == "Bearer private-test-token"  # type: ignore[attr-defined]
    assert (evidence / "desktop.log").read_text() == "[redacted]\n"
    cleanup.assert_called_once_with(process, [candidate])
    assert not (evidence / "receipt.json").exists(), "Only the successful caller writes a receipt"
