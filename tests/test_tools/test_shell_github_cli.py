"""The ordinary shell preserves full gh arguments and a workspace path with spaces."""

from __future__ import annotations

import json
import os
import shlex
import sys
from pathlib import Path

import pytest
from fastmcp import Client

from clio_agent import conf
from clio_agent.runtime import execution_environment as execution
from clio_agent.tools.execution import tool_workspace_context
from clio_agent.tools.servers.shell_server import shell_server


@pytest.mark.asyncio
async def test_managed_gh_uses_normal_shell_with_full_arguments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Use a test-only argv recorder, with the production MCP/shell/environment path."""
    workspace = tmp_path / "work space"
    workspace.mkdir()
    binary = tmp_path / "managed cli"
    binary.mkdir()
    recorder = binary / "argv.py"
    recorder.write_text("import json, sys\nprint(json.dumps(sys.argv[1:]))\n", encoding="utf-8")
    executable = binary / ("gh.cmd" if os.name == "nt" else "gh")
    if os.name == "nt":
        executable.write_text(
            f'@echo off\r\n"{sys.executable}" "{recorder}" %*\r\n', encoding="utf-8"
        )
    else:
        executable.write_text(
            f'#!/bin/sh\nexec {shlex.join([sys.executable, str(recorder)])} "$@"\n',
            encoding="utf-8",
        )
        executable.chmod(0o755)
    host_which = execution.shutil.which

    def which(command: str, mode: int = os.F_OK | os.X_OK, path: str | None = None) -> str | None:
        return None if command == "gh" else host_which(command, mode=mode, path=path)

    monkeypatch.setattr(execution.shutil, "which", which)
    monkeypatch.setattr(execution, "installed_github_cli", lambda: executable)
    monkeypatch.setenv("CLIO_ALLOWED_ROOTS", str(workspace))
    conf.reload()
    try:
        with tool_workspace_context(str(workspace)):
            async with Client(shell_server) as client:
                result = await client.call_tool(
                    "bash",
                    {
                        "command": 'gh repo clone iowarp/clio-agent "local checkout" -- --depth 1',
                        "timeout_s": 10,
                    },
                )
        data = result.data
        assert isinstance(data, dict)
        assert data["exit_code"] == 0, data
        assert json.loads(data["stdout"]) == [
            "repo",
            "clone",
            "iowarp/clio-agent",
            "local checkout",
            "--",
            "--depth",
            "1",
        ]
        assert Path(data["cwd"]).resolve() == workspace.resolve()
    finally:
        conf.reload()
