"""CLIO's document machinery in a workspace stays out of the person's git and searches."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from clio_agent.runtime.document_stack import process


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
def test_git_add_all_skips_document_scratch(tmp_path: Path) -> None:
    workspace = tmp_path / "project"
    workspace.mkdir()
    (workspace / "analysis.py").write_text("print('hi')\n", encoding="utf-8")
    scratch = process.scratch_root(workspace)
    modules = scratch / "javascript" / "node_modules" / "docx"
    modules.mkdir(parents=True)
    (modules / "index.js").write_text("module.exports = {};\n", encoding="utf-8")

    subprocess.run(["git", "init", "-q"], cwd=workspace, check=True)
    subprocess.run(["git", "add", "-A"], cwd=workspace, check=True)
    staged = subprocess.run(
        ["git", "diff", "--cached", "--name-only"],
        cwd=workspace,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.split()

    assert staged == ["analysis.py"]


def test_existing_markers_are_kept(tmp_path: Path) -> None:
    directory = tmp_path / "machinery"
    directory.mkdir()
    (directory / ".gitignore").write_text("custom\n", encoding="utf-8")

    process.mark_machinery_ignored(directory)

    assert (directory / ".gitignore").read_text(encoding="utf-8") == "custom\n"
    assert (directory / ".ignore").read_text(encoding="utf-8") == process.SEARCH_IGNORE_MARKER
