"""Blueprint git steps wait while git works and fail typed when it stalls (#1577 3.11).

REAL child processes stand in for git (a slow-but-working step, a stalled one, a failing
one); one test runs the real ``git`` against a local repository.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from clio_agent.gact import blueprint_git
from clio_agent.gact.blueprint_git import BlueprintGitStalledError, run_git


@pytest.fixture(autouse=True)
def _short_first_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(blueprint_git, "GIT_FIRST_WAIT_S", 1.0)


def test_a_slow_but_working_step_completes() -> None:
    """SABOTAGE: a flat ``subprocess.run(timeout=GIT_FIRST_WAIT_S)`` -> TimeoutExpired -> red."""
    busy = "import time\nend = time.monotonic() + 3.0\nwhile time.monotonic() < end: pass\n"
    result = run_git([sys.executable, "-c", busy + "print('cloned')"])
    assert result.stdout.strip() == "cloned"


def test_a_stalled_step_is_a_typed_value_error() -> None:
    """SABOTAGE: let the probe error escape untyped -> not a ValueError -> red."""
    with pytest.raises(BlueprintGitStalledError) as caught:
        run_git([sys.executable, "-c", "import time; time.sleep(30)"])
    assert isinstance(caught.value, ValueError)  # the install routes answer it as a 400
    assert caught.value.reason == "blueprint_git_no_progress"


def test_a_failing_step_raises_called_process_error() -> None:
    with pytest.raises(subprocess.CalledProcessError) as caught:
        run_git([sys.executable, "-c", "import sys; sys.exit(3)"])
    assert caught.value.returncode == 3


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_real_git_clone_of_a_local_repository(tmp_path: Path) -> None:
    source = tmp_path / "src"
    source.mkdir()
    (source / "AGENT.md").write_text("# agent\n", encoding="utf-8")
    for step in (
        ["git", "init", "-q", str(source)],
        ["git", "-C", str(source), "add", "."],
        [
            "git",
            "-C",
            str(source),
            "-c",
            "user.email=t@t",
            "-c",
            "user.name=t",
            "commit",
            "-qm",
            "init",
        ],
    ):
        run_git(step)
    run_git(["git", "clone", "--depth", "1", source.as_uri(), str(tmp_path / "clone")])
    assert (tmp_path / "clone" / "AGENT.md").is_file()
