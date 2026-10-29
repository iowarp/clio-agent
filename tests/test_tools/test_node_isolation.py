"""An isolated workflow node writes only its own folder; a fork's folder starts as a copy.

The narrowing is one territory source read by both boundaries (the file policy and
the shell's fence). The clone paths for macOS and Windows run here with the platform
call faked; they are qualified live on Linux only.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from clio_agent.gact.artifacts.designation import ground_output_paths, node_output_refusal
from clio_agent.gact.node_folders import (
    FOLDER_COPY_KEY,
    NODE_FOLDER_KEY,
    child_node_metadata,
    isolation_refusal,
    run_folder,
    step_folder,
)
from clio_agent.runtime import file_clone, sandbox
from clio_agent.runtime.sandbox_roots import PROFILE_FLEET, PROFILE_SHELL, effective_write_roots
from clio_agent.tools.execution import tool_workspace_context
from clio_agent.tools.file_policy import FileAccessPolicy, FilePolicyError
from clio_agent.tools.write_scope import tool_write_root_context


@pytest.fixture
def node(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    workspace = (tmp_path / "ws").resolve()
    workspace.mkdir()
    monkeypatch.setenv("CLIO_ALLOWED_ROOTS", str(workspace))
    folder = Path(step_folder(run_folder(workspace, "flowrun_1"), "measure"))
    folder.mkdir()
    return workspace, folder


def test_a_node_writes_its_folder_and_reads_the_whole_workspace(node: tuple[Path, Path]) -> None:
    workspace, folder = node
    (workspace / "data.csv").write_text("x\n1\n", encoding="utf-8")
    with tool_workspace_context(workspace), tool_write_root_context(folder):
        policy = FileAccessPolicy.from_env()
        assert policy.write_roots[0] == folder
        assert policy.validate_write("results.csv") == folder / "results.csv"
        assert policy.validate_read(str(workspace / "data.csv")) == workspace / "data.csv"
        with pytest.raises(FilePolicyError) as outside:
            policy.validate_write(str(workspace / "results.csv"))
        assert outside.value.code == "outside_allowed_roots"
        sibling = folder.parent / "other"
        sibling.mkdir()
        with pytest.raises(FilePolicyError):
            policy.validate_write(str(sibling / "x.txt"))
    # Outside the node's call, the workspace is writable as before.
    with tool_workspace_context(workspace):
        assert FileAccessPolicy.from_env().validate_write(str(workspace / "results.csv"))


def test_the_shell_fence_narrows_with_the_policy_and_the_fleet_does_not(
    node: tuple[Path, Path],
) -> None:
    workspace, folder = node
    with tool_workspace_context(workspace), tool_write_root_context(folder):
        shell = effective_write_roots(PROFILE_SHELL, workspace_root=str(workspace))
        fleet = effective_write_roots(PROFILE_FLEET, workspace_root=str(workspace))
        from clio_agent.tools.servers.shell_server import _resolve_cwd

        assert _resolve_cwd(None) == folder
    assert shell[0] == folder and workspace not in shell
    assert not any(root == workspace.parent for root in shell)
    # The fleet serves every session of the workspace, so it is never narrowed per node.
    assert workspace in fleet


def test_declared_output_paths_are_grounded_in_the_node_and_checked(
    node: tuple[Path, Path],
) -> None:
    workspace, folder = node
    with tool_workspace_context(workspace), tool_write_root_context(folder):
        grounded = ground_output_paths({"output_path": "plot.png"}, {}, str(workspace))
        assert grounded["output_path"] == str(folder / "plot.png")
        assert node_output_refusal(grounded) == ""
        refusal = node_output_refusal({"output_path": str(workspace / "plot.png")})
        assert "outside_allowed_roots" in refusal and str(folder) in refusal
    assert node_output_refusal({"output_path": str(workspace / "plot.png")}) == ""


def test_steps_children_and_forks_get_their_folders(node: tuple[Path, Path]) -> None:
    _workspace, folder = node
    (folder / "notes.txt").write_text("half", encoding="utf-8")
    (folder / "sub").mkdir()
    (folder / "sub" / "deep.bin").write_bytes(b"\x00" * 10)
    step = {NODE_FOLDER_KEY: str(folder)}
    assert child_node_metadata({}, dict(step), None, "measure") == step
    child = child_node_metadata(step, {"subagent_name": "explore"}, None, "explore")
    again = child_node_metadata(step, {"subagent_name": "explore"}, None, "explore")
    assert Path(child[NODE_FOLDER_KEY]) == folder / "explore"
    assert Path(again[NODE_FOLDER_KEY]) == folder / "explore-2"
    fork = child_node_metadata({}, {"subagent_name": "measure~1"}, step, "measure (fork)")
    forked = Path(fork[NODE_FOLDER_KEY])
    assert forked == folder.parent / "measure~1"
    assert (forked / "notes.txt").read_text(encoding="utf-8") == "half"
    assert (forked / "sub" / "deep.bin").stat().st_size == 10
    record = fork[FOLDER_COPY_KEY]
    assert record["method"] in {"clone", "copy"} and record["cloned"] + record["copied"] == 2
    # A fork of a node that is not a step is named beside it.
    nested = child_node_metadata({}, {"subagent_name": "explore (fork)"}, child, "x")
    assert Path(nested[NODE_FOLDER_KEY]).name == "explore~1"
    # No isolated source and no isolated parent: no folder.
    assert child_node_metadata({}, {}, None, "plain") == {}


def test_an_isolated_run_needs_an_active_fence(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sandbox, "current_state", lambda: None)
    refused = isolation_refusal()
    assert refused is not None and refused["reason"] == "isolation_unconfined"
    active = sandbox.SandboxResult(
        mechanism=sandbox.MECHANISM_LANDLOCK, active=True, reason="fence_active", details={}
    )
    monkeypatch.setattr(sandbox, "current_state", lambda: active)
    assert isolation_refusal() is None


def _never_link(*_args: Any, **_kwargs: Any) -> None:
    raise AssertionError("a fork's copy is never a hard link")


def test_macos_clones_with_clonefile_and_falls_back_to_a_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(os, "link", _never_link)
    source = tmp_path / "a.dat"
    source.write_bytes(b"abc")
    cloned: list[tuple[Path, Path]] = []

    def clonefile(src: Path, dst: Path) -> None:
        cloned.append((src, dst))
        dst.write_bytes(src.read_bytes())

    monkeypatch.setattr(file_clone, "_clone_macos", clonefile)
    assert file_clone.clone_or_copy_file(source, tmp_path / "b.dat", platform="darwin") == "clone"
    assert cloned == [(source, tmp_path / "b.dat")]

    def unsupported(src: Path, dst: Path) -> None:
        raise OSError(45, "Operation not supported")  # ENOTSUP on a non-APFS volume

    monkeypatch.setattr(file_clone, "_clone_macos", unsupported)
    assert file_clone.clone_or_copy_file(source, tmp_path / "c.dat", platform="darwin") == "copy"
    assert (tmp_path / "c.dat").read_bytes() == b"abc"


def test_windows_block_clones_whole_clusters_then_trims(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(os, "link", _never_link)
    source = tmp_path / "a.dat"
    source.write_bytes(b"x" * 5000)
    calls: list[int] = []

    def duplicate(src: Any, dst: Any, length: int) -> None:
        calls.append(length)
        dst.seek(0)
        dst.write(src.read())

    monkeypatch.setattr(file_clone, "_windows_cluster_size", lambda _path: 4096)
    monkeypatch.setattr(file_clone, "_duplicate_extents", duplicate)
    target = tmp_path / "b.dat"
    assert file_clone.clone_or_copy_file(source, target, platform="win32") == "clone"
    assert calls == [8192]  # rounded up to whole clusters
    assert target.read_bytes() == b"x" * 5000  # trimmed back to the source's size

    def refused(*_args: Any) -> None:
        raise OSError(50, "The request is not supported")  # NTFS: no block cloning

    monkeypatch.setattr(file_clone, "_duplicate_extents", refused)
    other = tmp_path / "c.dat"
    assert file_clone.clone_or_copy_file(source, other, platform="win32") == "copy"
    assert other.read_bytes() == b"x" * 5000


def test_a_tree_copy_counts_the_methods_and_keeps_symlinks(tmp_path: Path) -> None:
    source = tmp_path / "src"
    (source / "d").mkdir(parents=True)
    (source / "d" / "f.txt").write_text("1", encoding="utf-8")
    (source / "link").symlink_to("d/f.txt")
    copied = file_clone.clone_or_copy_tree(source, tmp_path / "dst", platform="other")
    assert copied == {"method": "copy", "cloned": 0, "copied": 1}
    assert (tmp_path / "dst" / "link").is_symlink()
    assert os.readlink(tmp_path / "dst" / "link") == "d/f.txt"
    # On this Linux host the real FICLONE path either clones or falls back to a copy.
    assert file_clone.clone_or_copy_tree(source, tmp_path / "dst2")["method"] in {"clone", "copy"}
