"""The per-port credential record the Desktop reads to attach to a running CLIO (#1478)."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest
import uvicorn
from fastapi import FastAPI

from clio_agent.gact import desktop_lifecycle
from clio_agent.gact.server_credentials import (
    credential_record_path,
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
