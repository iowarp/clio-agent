"""A fenced MCP server can move the conversation's files by path, and nothing wider.

Relay's ``files`` upload and download open ``local_path`` with the server's own launch, so
the fleet fence must let it write the workspace and the attachments' working copies
(:mod:`clio_agent.gact.resource_materialization`) and must not grant an unrelated folder.
Reads are the fence's ``:root`` reads minus storage protections (sandbox_codex_profile).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from clio_agent import paths
from clio_agent.platform_paths import path_is_within
from clio_agent.runtime import sandbox
from clio_agent.runtime.session_scratch import system_temp_roots
from clio_agent.tools.file_policy import FileAccessPolicy
from clio_agent.tools.mcp_confine import confine_stdio_server


def test_a_fleet_server_writes_its_workspace_and_attachments_only(
    tmp_path: Path, tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = (tmp_path / "ws").resolve()
    workspace.mkdir()
    unrelated = tmp_path_factory.mktemp("elsewhere").resolve()

    # The advisory roots are the workspace alone, so only the fence's own grants remain.
    monkeypatch.setattr(
        FileAccessPolicy, "from_env", classmethod(lambda cls: cls(allowed_roots=(workspace,)))
    )
    seen: dict[str, Any] = {}
    real = sandbox.wrap_confined

    def capture(command: str, args: list[str], **kwargs: Any) -> Any:
        seen.update(kwargs)
        return real(command, args, **kwargs)

    monkeypatch.setattr(sandbox, "wrap_confined", capture)
    confine_stdio_server("relay", "relay", ["serve"], {}, str(workspace))

    roots = tuple(Path(root) for root in seen["write_roots"])
    attachment = paths.workspace_state_dir(workspace) / "inputs" / "res_abc" / "mesh.stl"
    assert seen["cwd"] == str(workspace)
    assert any(path_is_within(workspace / "downloads" / "out.stl", root) for root in roots)
    # The test's temp folder holds tmp_path; temp is a grant of its own, not of this scope.
    scoped = [root for root in roots if root not in system_temp_roots()]
    assert not any(path_is_within(unrelated / "out.stl", root) for root in scoped)
    assert any(path_is_within(attachment, root) for root in scoped)
