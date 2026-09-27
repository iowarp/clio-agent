"""The per-port credential record the Desktop reads to attach to a running CLIO (#1478)."""

from __future__ import annotations

import json
import logging
import os
import socket
import stat
import subprocess
import sys
from pathlib import Path

import pytest
import uvicorn
from fastapi import FastAPI

from clio_agent.gact import desktop_lifecycle
from clio_agent.gact.server_credentials import (
    credential_record_path,
    prune_stale_server_credentials,
    publish_server_credentials,
    remove_server_credentials,
)


def test_record_lives_in_the_runtime_state_dir_the_desktop_also_resolves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLIO_RUNTIME_STATE_DIR", str(tmp_path))

    assert credential_record_path(17800) == tmp_path / "gact-servers" / "17800.json"


def test_publish_writes_port_pid_and_token_owner_only(tmp_path: Path) -> None:
    path = publish_server_credentials(17800, "desktop-token", pid=4242, state_dir=tmp_path)

    assert json.loads(path.read_text(encoding="utf-8")) == {
        "schema": 1,
        "port": 17800,
        "pid": 4242,
        "bearer_token": "desktop-token",
    }
    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert [entry.name for entry in path.parent.iterdir()] == ["17800.json"]


def test_a_server_without_a_token_publishes_null(tmp_path: Path) -> None:
    path = publish_server_credentials(17800, None, pid=1, state_dir=tmp_path)

    assert json.loads(path.read_text(encoding="utf-8"))["bearer_token"] is None


def test_remove_leaves_a_newer_servers_record_alone(tmp_path: Path) -> None:
    publish_server_credentials(17800, "new", pid=2, state_dir=tmp_path)

    assert remove_server_credentials(17800, pid=1, state_dir=tmp_path) is False
    assert credential_record_path(17800, state_dir=tmp_path).exists()
    assert remove_server_credentials(17800, pid=2, state_dir=tmp_path) is True
    assert not credential_record_path(17800, state_dir=tmp_path).exists()


def test_serve_foreground_publishes_the_enforced_token_while_serving(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLIO_RUNTIME_STATE_DIR", str(tmp_path))
    record = tmp_path / "gact-servers" / "8123.json"
    seen_while_serving: list[dict[str, object]] = []

    def _fake_run(self: uvicorn.Server) -> None:
        seen_while_serving.append(json.loads(record.read_text(encoding="utf-8")))

    monkeypatch.setattr(uvicorn.Server, "run", _fake_run)
    app = FastAPI()
    app.state.bearer_token = "launcher-token"

    desktop_lifecycle.serve_foreground(app, host="127.0.0.1", port=8123)

    assert seen_while_serving == [
        {"schema": 1, "port": 8123, "pid": os.getpid(), "bearer_token": "launcher-token"}
    ]
    assert not record.exists(), "the record must not outlive the server"


def _write_record(state_dir: Path, name: str, payload: object) -> Path:
    path = state_dir / "gact-servers" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload if isinstance(payload, str) else json.dumps(payload), "utf-8")
    return path


def _dead_pid() -> int:
    """A pid that is certainly not running: a child that already exited."""

    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    return child.pid


def _record(port: int, pid: int) -> dict[str, object]:
    return {"schema": 1, "port": port, "pid": pid, "bearer_token": None}


def test_prune_removes_records_of_dead_servers_with_a_typed_reason(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    dead = _write_record(tmp_path, "17962.json", _record(17962, _dead_pid()))
    torn = _write_record(tmp_path, "17990.json", "{not json")
    live = _write_record(tmp_path, "63645.json", _record(63645, os.getpid()))

    with caplog.at_level(logging.INFO, logger="clio_agent.gact.server_credentials"):
        pruned = prune_stale_server_credentials(
            state_dir=tmp_path, port_listening=lambda _port: False
        )

    assert sorted((item.port, item.reason) for item in pruned) == [
        (17962, "dead_pid"),
        (17990, "unreadable"),
    ]
    assert not dead.exists()
    assert not torn.exists()
    assert live.exists(), "a record whose server is alive is never touched"
    logged = [record.getMessage() for record in caplog.records]
    assert any("reason=dead_pid" in line and "port=17962" in line for line in logged)
    assert any("reason=unreadable" in line and "port=17990" in line for line in logged)


def test_prune_keeps_a_record_whose_port_still_answers(tmp_path: Path) -> None:
    record = _write_record(tmp_path, "18961.json", _record(18961, _dead_pid()))

    pruned = prune_stale_server_credentials(state_dir=tmp_path, port_listening=lambda _port: True)

    assert pruned == []
    assert record.exists(), "something still serves that port, so the record may be its"


def test_prune_ignores_staging_files_and_a_missing_directory(tmp_path: Path) -> None:
    assert prune_stale_server_credentials(state_dir=tmp_path) == []
    staging = _write_record(tmp_path, ".17800.json.4242.tmp", "{")

    assert prune_stale_server_credentials(state_dir=tmp_path) == []
    assert staging.exists()


def test_prune_probes_the_real_port_by_default(tmp_path: Path) -> None:
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = listener.getsockname()[1]
    try:
        kept = _write_record(tmp_path, f"{port}.json", _record(port, _dead_pid()))
        assert prune_stale_server_credentials(state_dir=tmp_path) == []
        assert kept.exists()
    finally:
        listener.close()

    assert [item.reason for item in prune_stale_server_credentials(state_dir=tmp_path)] == [
        "dead_pid"
    ]


def test_serve_foreground_prunes_stale_records_before_publishing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLIO_RUNTIME_STATE_DIR", str(tmp_path))
    stale = _write_record(tmp_path, "17962.json", _record(17962, _dead_pid()))
    monkeypatch.setattr(uvicorn.Server, "run", lambda self: None)

    desktop_lifecycle.serve_foreground(FastAPI(), host="127.0.0.1", port=8124)

    assert not stale.exists()
