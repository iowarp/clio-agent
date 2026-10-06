"""Explicit Git publication commits only selected authoring paths and preserves other work."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from clio_agent.gact.blueprint_git_publish import publish_git_changes


def _git(root: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


def test_publish_keeps_unrelated_staged_work_and_push_is_explicit(tmp_path: Path) -> None:
    repository = tmp_path / "checkout"
    repository.mkdir()
    _git(repository, "init", "-b", "main")
    _git(repository, "config", "user.name", "Blueprint Test")
    _git(repository, "config", "user.email", "blueprint-test@example.invalid")
    blueprint = repository / "demo"
    blueprint.mkdir()
    (blueprint / "AGENT.md").write_text("Before\n")
    (repository / "unrelated.txt").write_text("Before\n")
    _git(repository, "add", ".")
    _git(repository, "commit", "-m", "test: seed checkout")
    remote = tmp_path / "remote.git"
    _git(tmp_path, "init", "--bare", str(remote))
    _git(repository, "remote", "add", "origin", str(remote))
    _git(repository, "push", "-u", "origin", "main")
    original = _git(remote, "rev-parse", "main")
    (repository / "unrelated.txt").write_text("Other work\n")
    _git(repository, "add", "unrelated.txt")
    (blueprint / "AGENT.md").write_text("Reviewed change\n")
    result = publish_git_changes(
        blueprint, ["AGENT.md"], message="feat: reviewed blueprint", push=False
    )
    assert result["pushed"] is False
    assert _git(repository, "show", "--format=", "--name-only", "HEAD") == "demo/AGENT.md"
    assert _git(repository, "diff", "--cached", "--name-only") == "unrelated.txt"
    assert _git(remote, "rev-parse", "main") == original
    retry = publish_git_changes(
        blueprint, ["AGENT.md"], message="feat: reviewed blueprint", push=True
    )
    assert retry["commit"] == result["commit"]
    assert _git(remote, "rev-parse", "main") == result["commit"]
    assert _git(repository, "diff", "--cached", "--name-only") == "unrelated.txt"


def test_blank_message_refuses_publication(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="commit message"):
        publish_git_changes(tmp_path, [], message=" ", push=True)
