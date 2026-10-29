"""Approved source reads reach the child fence without acquiring write grants."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from clio_agent import conf
from clio_agent.gact.folder_access import requested_folders
from clio_agent.runtime import storage_access
from clio_agent.runtime.sandbox_codex_profile import synthesize_codex_profile
from clio_agent.runtime.sandbox_roots import granted_write_roots
from clio_agent.tools.execution import tool_workspace_context
from clio_agent.tools.file_policy import FileAccessPolicy, FilePolicyError
from clio_agent.tools.servers.shell_server import _resolve_cwd, shell_server


@pytest.fixture
def source_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[Path, Path]]:
    """Persist an approved read-only snapshot outside the workspace's write roots."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    data = tmp_path / "data"
    snapshot = data / "connected-sources" / "source_test" / "snapshot_test"
    snapshot.mkdir(parents=True)
    (snapshot / "data.csv").write_text("x,y\n1,2\n3,4\n", encoding="utf-8")
    record = {
        "workspace_root": str(workspace),
        "manifest_id": "snapshot_test",
        "connected": True,
        "source": {
            "id": "source_test",
            "provider": "ndp",
            "local_path": str(snapshot),
            "materialization": "ready",
        },
    }
    with closing(sqlite3.connect(data / "connected-sources" / "sources.sqlite3")) as db, db:
        db.execute("CREATE TABLE records (kind TEXT, body TEXT)")
        db.execute("INSERT INTO records VALUES ('source', ?)", (json.dumps(record),))
    monkeypatch.setattr(storage_access.paths, "user_data_dir", lambda: data)
    monkeypatch.setattr(storage_access.paths, "user_config_dir", lambda: tmp_path / "config")
    monkeypatch.setenv("CLIO_ALLOWED_ROOTS", str(workspace))
    monkeypatch.setitem(
        conf.store().data["tools"]["file_policy"], "allowed_roots", [str(workspace)]
    )
    with tool_workspace_context(workspace):
        yield workspace, snapshot


@pytest.mark.parametrize("location", ["file", "directory", "cwd", "reader"])
def test_approved_read_does_not_request_writable_folder(
    source_paths: tuple[Path, Path], location: str
) -> None:
    workspace, snapshot = source_paths
    path = snapshot / "data.csv" if location in {"file", "reader"} else snapshot
    name = "fs_read_file" if location == "reader" else "shell_bash"
    args = (
        {"filepath": str(path)}
        if location == "reader"
        else {"cwd": str(path)}
        if location == "cwd"
        else {"cwd": str(workspace), "access_paths": [str(path)]}
    )
    assert requested_folders(name, args) == []
    assert granted_write_roots(str(workspace)) == ()
    policy = FileAccessPolicy.from_env()
    assert snapshot not in policy.allowed_roots
    assert policy.validate_read(str(snapshot / "data.csv")) == snapshot / "data.csv"
    assert _resolve_cwd(str(snapshot)) == snapshot
    with pytest.raises(FilePolicyError, match="read-only"):
        policy.validate_write(str(snapshot / "data.csv"))


@pytest.mark.parametrize("name", ["fs_write_file", "fs_edit_file"])
def test_edit_tools_still_reject_read_only_source(
    source_paths: tuple[Path, Path], name: str
) -> None:
    _, snapshot = source_paths
    with pytest.raises(FilePolicyError, match="read-only"):
        requested_folders(name, {"filepath": str(snapshot / "data.csv")})


def test_other_workspace_and_source_ancestor_cannot_acquire_write_access(
    source_paths: tuple[Path, Path], tmp_path: Path
) -> None:
    _, snapshot = source_paths
    other_workspace = tmp_path / "other"
    other_workspace.mkdir()
    # A source's ancestor is not an approved read root for this workspace.
    with pytest.raises(FilePolicyError, match="read-only"):
        requested_folders("shell_bash", {"access_paths": [str(snapshot.parent)]})
    with tool_workspace_context(other_workspace):
        assert not FileAccessPolicy((other_workspace,)).allows_source_read(snapshot)
    assert granted_write_roots(str(other_workspace)) == ()


def test_credentials_stay_private_even_when_declared(
    source_paths: tuple[Path, Path], tmp_path: Path
) -> None:
    _, snapshot = source_paths
    private = tmp_path / "config" / "storage-auth"
    private.mkdir(parents=True)
    secret = private / "credentials.json"
    secret.write_text("test-only", encoding="utf-8")
    with pytest.raises(FilePolicyError, match="private"):
        requested_folders("shell_bash", {"access_paths": [str(snapshot), str(secret)]})
    with pytest.raises(FilePolicyError, match="private"):
        _resolve_cwd(str(private))


def test_source_directory_does_not_become_writable_in_child_profile(
    source_paths: tuple[Path, Path],
) -> None:
    workspace, snapshot = source_paths
    # Even an overlapping grant cannot override the protected source ancestor.
    filesystem = synthesize_codex_profile([workspace, snapshot])["filesystem"]
    assert filesystem[str(workspace)] == "write"
    assert filesystem[str(snapshot.parent.parent)] == "read"
    assert str(snapshot) not in filesystem


@pytest.mark.asyncio
@pytest.mark.usefixtures("floor_sandbox")
async def test_shell_still_refuses_to_run_when_source_fence_is_inactive(
    source_paths: tuple[Path, Path],
) -> None:
    """The suite's floor sandbox must reject before spawning arbitrary child code."""
    workspace, snapshot = source_paths
    async with Client(shell_server) as client:
        with pytest.raises(ToolError, match="cannot enforce connected-source exclusions"):
            await client.call_tool(
                "bash",
                {
                    "command": "echo must-not-run",
                    "cwd": str(workspace),
                    "access_paths": [str(snapshot)],
                },
            )
    assert (snapshot / "data.csv").read_text(encoding="utf-8") == "x,y\n1,2\n3,4\n"
