"""Running install.sh --source again over its own clone upgrades or repairs it.

The clone mode installs into ``$CLIO_PREFIX/clio-agent``. A re-run fetches the requested
ref into that clone and continues; a clone an interrupted install left unfinished is
cloned again; anything that could hold a person's work (local changes, another origin,
files that are not a clone) is refused with the cause and the fix, and left untouched.

git is real here: the CLIO repository URL is redirected (``url.<local>.insteadOf``) to a
local repository, so the clone keeps the real origin URL the installer checks.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.test_scripts.installer_shims import ROOT, STOP, run_installer

pytestmark = pytest.mark.skipif(
    os.name == "nt" or shutil.which("git") is None, reason="needs bash and git"
)

CLIO_REPO = "https://github.com/iowarp/clio-agent.git"


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def _origin(tmp_path: Path) -> Path:
    """A local stand-in for the CLIO repository, with one commit on main."""
    origin = tmp_path / "origin"
    origin.mkdir()
    _git("init", "-q", "-b", "main", cwd=origin)
    _git("config", "user.email", "t@example.com", cwd=origin)
    _git("config", "user.name", "t", cwd=origin)
    (origin / ".python-version").write_text("3.14\n")
    (origin / "README.md").write_text("one\n")
    _git("add", ".", cwd=origin)
    _git("commit", "-q", "-m", "one", cwd=origin)
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    (home / ".gitconfig").write_text(
        f'[url "file://{origin}"]\n\tinsteadOf = {CLIO_REPO}\n[protocol "file"]\n\tallow = always\n'
    )
    return origin


def _commit(origin: Path, text: str) -> str:
    (origin / "README.md").write_text(text)
    _git("commit", "-q", "-am", text.strip(), cwd=origin)
    return _git("rev-parse", "HEAD", cwd=origin)


def _install(tmp_path: Path) -> subprocess.CompletedProcess[str]:
    streamed = tmp_path / "streamed" / "install.sh"  # not inside a checkout: clone mode
    streamed.parent.mkdir(exist_ok=True)
    shutil.copy(ROOT / "install/install.sh", streamed)
    result, _calls = run_installer(tmp_path, "--source", script=streamed, real_git=True)
    return result


def test_a_rerun_updates_the_clone_to_the_ref(tmp_path: Path) -> None:
    origin = _origin(tmp_path)
    clone = tmp_path / "prefix" / "clio-agent"
    first = _install(tmp_path)
    assert first.returncode == STOP, first.stdout + first.stderr  # stops at uv sync
    head = _commit(origin, "two\n")
    second = _install(tmp_path)
    assert second.returncode == STOP, second.stdout + second.stderr
    assert "Updating the clio-agent clone" in second.stdout
    assert _git("rev-parse", "HEAD", cwd=clone) == head
    assert (clone / "README.md").read_text() == "two\n"


def test_an_interrupted_clone_is_cloned_again(tmp_path: Path) -> None:
    _origin(tmp_path)
    clone = tmp_path / "prefix" / "clio-agent"
    clone.mkdir(parents=True)
    _git("init", "-q", cwd=clone)  # what an install killed mid-clone leaves: no commit
    _git("remote", "add", "origin", CLIO_REPO, cwd=clone)
    result = _install(tmp_path)
    assert result.returncode == STOP, result.stdout + result.stderr
    assert "left unfinished" in result.stderr
    assert (clone / "README.md").read_text() == "one\n"


def test_local_changes_refuse_the_rerun_and_are_kept(tmp_path: Path) -> None:
    _origin(tmp_path)
    clone = tmp_path / "prefix" / "clio-agent"
    assert _install(tmp_path).returncode == STOP
    (clone / "README.md").write_text("my work\n")
    result = _install(tmp_path)
    assert result.returncode == 1
    assert "Source reinstall refused" in result.stderr and "local changes" in result.stderr
    assert "README.md" in result.stderr  # the cause names what changed
    assert (clone / "README.md").read_text() == "my work\n"


def test_a_clone_of_another_repository_is_refused(tmp_path: Path) -> None:
    _origin(tmp_path)
    clone = tmp_path / "prefix" / "clio-agent"
    clone.mkdir(parents=True)
    _git("init", "-q", cwd=clone)
    _git("remote", "add", "origin", "https://example.com/someone/else.git", cwd=clone)
    (clone / "notes.txt").write_text("keep")
    result = _install(tmp_path)
    assert result.returncode == 1
    assert "is a clone of https://example.com/someone/else.git" in result.stderr
    assert (clone / "notes.txt").read_text() == "keep"


def test_a_release_install_environment_is_replaced(tmp_path: Path) -> None:
    _origin(tmp_path)
    clone = tmp_path / "prefix" / "clio-agent"
    (clone / ".venv" / "bin").mkdir(parents=True)  # a release install: only its env
    result = _install(tmp_path)
    assert result.returncode == STOP, result.stdout + result.stderr
    assert (clone / "README.md").read_text() == "one\n"


def _origin_with_submodule(tmp_path: Path) -> Path:
    """The stand-in repository with a submodule at ``external/sub``."""
    sub = tmp_path / "sub"
    sub.mkdir()
    _git("init", "-q", "-b", "main", cwd=sub)
    _git("config", "user.email", "t@example.com", cwd=sub)
    _git("config", "user.name", "t", cwd=sub)
    (sub / "tool.txt").write_text("tool\n")
    _git("add", ".", cwd=sub)
    _git("commit", "-q", "-m", "tool", cwd=sub)
    origin = _origin(tmp_path)
    _git(
        "-c", "protocol.file.allow=always",
        "submodule", "add", "-q", f"file://{sub}", "external/sub",
        cwd=origin,
    )  # fmt: skip
    _git("commit", "-q", "-m", "submodule", cwd=origin)
    return origin


def test_a_submodule_left_at_another_commit_is_repaired(tmp_path: Path) -> None:
    """An interrupted clone can leave a submodule at another commit, nothing changed in it."""
    _origin_with_submodule(tmp_path)
    clone = tmp_path / "prefix" / "clio-agent"
    assert _install(tmp_path).returncode == STOP
    recorded = _git("rev-parse", "HEAD", cwd=clone / "external/sub")
    _git("-c", "user.email=t@example.com", "-c", "user.name=t",
         "commit", "-q", "--allow-empty", "-m", "elsewhere", cwd=clone / "external/sub")  # fmt: skip
    result = _install(tmp_path)
    assert result.returncode == STOP, result.stdout + result.stderr
    assert _git("rev-parse", "HEAD", cwd=clone / "external/sub") == recorded


def test_changes_inside_a_submodule_refuse_the_rerun(tmp_path: Path) -> None:
    _origin_with_submodule(tmp_path)
    clone = tmp_path / "prefix" / "clio-agent"
    assert _install(tmp_path).returncode == STOP
    (clone / "external/sub/tool.txt").write_text("my work\n")
    result = _install(tmp_path)
    assert result.returncode == 1
    assert "external/sub" in result.stderr
    assert (clone / "external/sub/tool.txt").read_text() == "my work\n"
