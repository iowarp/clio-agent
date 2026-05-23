"""Tests for CLIO's local shell utility tool."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest
from fastmcp import Client

from clio_agent.tools.servers.shell_server import shell_server


def _parse_result(result: object) -> dict:
    data = getattr(result, "data", result)
    if isinstance(data, dict):
        return data
    if isinstance(data, str):
        return json.loads(data)
    raise AssertionError(f"unexpected result type: {type(data)!r}")


@pytest.mark.asyncio
async def test_shell_bash_runs_simple_command(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The shell tool should execute a bounded command and return output."""

    monkeypatch.setenv("CLIO_ALLOWED_ROOTS", str(tmp_path))
    if os.name == "nt":
        command = f"& '{sys.executable}' -c \"print('CLIO_SHELL_OK')\""
    else:
        command = f"'{sys.executable}' -c \"print('CLIO_SHELL_OK')\""

    async with Client(shell_server) as client:
        result = await client.call_tool(
            "bash",
            {"command": command, "cwd": str(tmp_path), "timeout_s": 5},
        )

    data = _parse_result(result)
    assert data["exit_code"] == 0
    assert data["timed_out"] is False
    assert data["stdout"].strip() == "CLIO_SHELL_OK"
    assert data["stderr"] == ""


@pytest.mark.asyncio
async def test_shell_bash_rejects_cwd_outside_allowed_roots(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The shell working directory must obey the file policy."""

    allowed = tmp_path / "allowed"
    outside = tmp_path / "outside"
    allowed.mkdir()
    outside.mkdir()
    monkeypatch.setenv("CLIO_ALLOWED_ROOTS", str(allowed))

    async with Client(shell_server) as client:
        result = await client.call_tool(
            "bash",
            {"command": "echo should-not-run", "cwd": str(outside)},
        )

    data = _parse_result(result)
    assert data["error"]["type"] == "file_policy"
    assert data["error"]["code"] == "outside_allowed_roots"
