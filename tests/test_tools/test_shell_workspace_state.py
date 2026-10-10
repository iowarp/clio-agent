"""Standalone skill scripts receive the owning workspace's canonical storage path."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest
from fastmcp import Client

from clio_agent import conf, paths
from clio_agent.runtime import sandbox
from clio_agent.tools.execution import tool_workspace_context
from clio_agent.tools.servers.shell_server import shell_server


async def test_shell_passes_actual_cwd_to_fence_without_reordering_grants(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    nested = workspace / "nested"
    nested.mkdir(parents=True)
    monkeypatch.setenv("CLIO_ALLOWED_ROOTS", str(workspace))
    monkeypatch.setenv("CLIO_AGENT_STATE_DIR", str(tmp_path / "state"))
    seen: list[dict] = []
    wrap = sandbox.wrap_confined

    def record(*args: object, **kwargs: object) -> sandbox.ConfinedSpawn:
        seen.append(dict(kwargs))
        return wrap(*args, **kwargs)

    monkeypatch.setattr(sandbox, "wrap_confined", record)
    command = "Get-Location | Select-Object -ExpandProperty Path" if os.name == "nt" else "pwd"
    conf.reload()
    try:
        with tool_workspace_context(str(workspace)):
            expected_roots = sandbox.effective_write_roots(
                sandbox.PROFILE_SHELL, workspace_root=str(nested)
            )
            async with Client(shell_server) as client:
                result = await client.call_tool("bash", {"command": command, "cwd": str(nested)})
        value = json.loads(result.data) if isinstance(result.data, str) else result.data
        assert value["exit_code"] == 0
        assert Path(value["stdout"].strip()) == nested
        assert seen[0]["cwd"] == nested
        assert seen[0]["write_roots"] == expected_roots
        assert Path(expected_roots[0]) != nested
    finally:
        conf.reload()


async def test_shell_state_tracks_bound_workspace_not_cwd_or_inherited_value(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, subdirectory = tmp_path / "workspace", tmp_path / "workspace/subdir"
    subdirectory.mkdir(parents=True)
    other = tmp_path / "another-workspace"
    other.mkdir()
    monkeypatch.setenv("CLIO_ALLOWED_ROOTS", str(tmp_path))
    monkeypatch.setenv("CLIO_AGENT_STATE_DIR", str(tmp_path / "agent-state"))
    monkeypatch.setenv("CLIO_AGENT_WORKSPACE_STATE_DIR", str(tmp_path / "stale-other-host"))
    script = subdirectory / "state.py"
    script.write_text(
        "import os\nfrom pathlib import Path\n"
        "root = Path(os.environ['CLIO_AGENT_WORKSPACE_STATE_DIR'])\n"
        "root.mkdir(parents=True, exist_ok=True)\n"
        "(root / 'dataset-test.txt').write_text('owned state')\nprint(root)\n"
    )
    command = ("& " if os.name == "nt" else "") + f"'{sys.executable}' '{script}'"
    conf.reload()
    try:
        for root in (workspace, other):
            with tool_workspace_context(str(root)):
                async with Client(shell_server) as client:
                    result = await client.call_tool(
                        "bash", {"command": command, "cwd": str(subdirectory), "timeout_s": 30}
                    )
            value = result.data
            if isinstance(value, str):
                value = json.loads(value)
            assert value["exit_code"] == 0
            assert Path(value["stdout"].strip()) == paths.workspace_state_dir(root)
            assert (
                paths.workspace_state_dir(root) / "dataset-test.txt"
            ).read_text() == "owned state"
            assert not (root / ".clio").exists()
    finally:
        conf.reload()
