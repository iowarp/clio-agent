"""Resumable Clio Coder runs: further turns in the same session, cancellation, and landing.

Runs against the scripted ACP fake of ``test_clio_coder_delegation``: each turn is a fresh
``acp`` process that resumes the session the first turn opened, the way CLIO continues a run
from a later conversation turn.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path

import pytest

from clio_agent.runtime.clio_coder import (
    ClioCoderError,
    ClioCoderInstall,
    ContinueRequest,
    continue_run,
    delegate_coding_task,
    land_run,
)
from clio_agent.runtime.clio_coder.acp import allow_permissions_once
from clio_agent.runtime.clio_coder.runs import load_run, run_path
from tests.test_runtime.test_clio_coder_delegation import FAKE_CLIO_CODER, _branches, _request


@pytest.fixture
def install(tmp_path: Path) -> ClioCoderInstall:
    script = tmp_path / "fake_clio_coder.py"
    script.write_text(FAKE_CLIO_CODER, "utf-8")
    return ClioCoderInstall((sys.executable, "-B", str(script)))


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "repo"
    path.mkdir()
    (path / "calc.py").write_text("def add(a, b):\n    return a - b\n", "utf-8")
    _git(path, "init", "-q", "-b", "main")
    _git(path, "add", "calc.py")
    _git(path, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init")
    return path


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    ).stdout


async def _open_run(repo: Path, tmp_path: Path, install: ClioCoderInstall, task: str = "fix add"):
    request = _request(repo, tmp_path, install, task, surface="acp", resumable=True)
    return await delegate_coding_task(request, on_permission=allow_permissions_once)


def _continue(tmp_path: Path, install: ClioCoderInstall, run_id: str, task: str, **kw):
    return continue_run(
        ContinueRequest(
            run_id=run_id, task=task, state_root=tmp_path / "state", install=install, **kw
        ),
        on_permission=allow_permissions_once,
    )


def _sessions(tmp_path: Path) -> dict:
    return json.loads((tmp_path / "state" / "home" / "state" / "sessions.json").read_text())


async def test_a_run_continues_its_session_in_its_worktree_and_lands_verified_work(
    repo: Path, tmp_path: Path, install: ClioCoderInstall
) -> None:
    first = await _open_run(repo, tmp_path, install)
    assert (first.run_id, first.turn, first.verified) == (first.run_id, 1, True)
    assert first.run_id and first.commit

    events: list[dict] = []
    second = await continue_run(
        ContinueRequest(
            run_id=first.run_id,
            task="now add @SUB",
            state_root=tmp_path / "state",
            install=install,
        ),
        sink=events.append,
        on_permission=allow_permissions_once,
    )

    # Same session (resumed in a new process), same worktree and branch, one commit per turn.
    assert {"kind": "session", "sessionId": "s-acp", "resumed": True} in events
    assert _sessions(tmp_path)["s-acp"]["turns"] == 2
    assert (second.session_id, second.workspace, second.branch) == (
        "s-acp",
        first.workspace,
        first.branch,
    )
    assert second.turn == 2 and second.verified and second.commit != first.commit
    assert _git(repo, "rev-list", "--count", f"{first.base_commit}..{second.branch}") == "2\n"
    assert "+def sub(a, b):" in second.diff and "+    return a + b" in second.diff
    assert second.run_usage["input"] == first.usage["input"] + second.usage["input"]

    landed = land_run(tmp_path / "state", repo, first.run_id, "apply")

    assert landed.status == "landed" and landed.commit == second.commit
    assert "def sub(a, b)" in (repo / "calc.py").read_text()
    # Applied, not committed: the person commits it; the run's worktree and branch are gone.
    assert _git(repo, "status", "--porcelain").split() == ["M", "calc.py", "??", ".decision"]
    assert _branches(repo) == ["main"] and not Path(first.workspace).exists()
    assert not run_path(tmp_path / "state", first.run_id).with_suffix(".lock").exists()
    with pytest.raises(ClioCoderError, match="landed"):
        await _continue(tmp_path, install, first.run_id, "more")


async def test_a_run_without_a_change_has_nothing_to_land(
    repo: Path, tmp_path: Path, install: ClioCoderInstall
) -> None:
    (repo / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    (repo / ".gitignore").write_text(".decision\n")
    _git(repo, "add", ".gitignore")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qam", "fixed already")
    (repo / "notes.txt").write_text("unrelated uncommitted work")
    first = await _open_run(repo, tmp_path, install)
    assert first.verified and not first.changed_files
    with pytest.raises(ClioCoderError, match="changed nothing"):
        land_run(tmp_path / "state", repo, first.run_id, "apply")
    assert land_run(tmp_path / "state", repo, first.run_id, "discard").status == "discarded"


async def test_a_turn_starts_from_the_branch_and_may_replace_the_check(
    repo: Path, tmp_path: Path, install: ClioCoderInstall
) -> None:
    first = await _open_run(repo, tmp_path, install)
    worktree = Path(first.workspace)
    (worktree / "leftover.log").write_text("from the last check")
    stricter = [*first.verification.command[:-1], "import calc; assert calc.sub(5, 3) == 2"]

    failed = await _continue(tmp_path, install, first.run_id, "tidy up", verify_command=stricter)
    assert not failed.verified and "leftover.log" not in failed.changed_files

    fixed = await _continue(tmp_path, install, first.run_id, "add @SUB")
    record = load_run(tmp_path / "state", first.run_id)
    assert fixed.verified and record.verify_command == stricter
    assert [turn.verified for turn in record.turns] == [True, False, True]


async def test_landing_refuses_unaccepted_or_moved_work_and_discard_cleans_up(
    repo: Path, tmp_path: Path, install: ClioCoderInstall
) -> None:
    first = await _open_run(repo, tmp_path, install)
    broken = await _continue(tmp_path, install, first.run_id, "@BREAK it")
    assert broken.outcome == "succeeded" and not broken.verified
    with pytest.raises(ClioCoderError, match="acceptance check did not pass"):
        land_run(tmp_path / "state", repo, first.run_id, "apply")

    await _continue(tmp_path, install, first.run_id, "fix add again")
    worktree = Path(first.workspace)
    (worktree / "calc.py").write_text("def add(a, b):\n    return 0\n")
    _git(worktree, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qam", "sneak")
    with pytest.raises(ClioCoderError, match="branch moved"):
        land_run(tmp_path / "state", repo, first.run_id, "apply")
    assert "a - b" in (repo / "calc.py").read_text()

    discarded = land_run(tmp_path / "state", repo, first.run_id, "discard")
    assert discarded.status == "discarded" and _branches(repo) == ["main"]
    assert not worktree.exists() and "a - b" in (repo / "calc.py").read_text()
    with pytest.raises(ClioCoderError, match="discarded"):
        land_run(tmp_path / "state", repo, first.run_id, "apply")


async def test_landing_never_overwrites_uncommitted_edits_in_the_checkout(
    repo: Path, tmp_path: Path, install: ClioCoderInstall
) -> None:
    first = await _open_run(repo, tmp_path, install)
    (repo / "calc.py").write_text("# the person's own edit\n")
    with pytest.raises(ClioCoderError, match="uncommitted changes"):
        land_run(tmp_path / "state", repo, first.run_id, "apply")
    assert (repo / "calc.py").read_text() == "# the person's own edit\n"
    assert load_run(tmp_path / "state", first.run_id).status == "open"


async def test_a_cancelled_turn_drops_its_partial_edits_and_keeps_the_run_open(
    repo: Path, tmp_path: Path, install: ClioCoderInstall
) -> None:
    first = await _open_run(repo, tmp_path, install)
    worktree = Path(first.workspace)
    work = asyncio.ensure_future(_continue(tmp_path, install, first.run_id, "@DIRTY then @HANG"))
    for _ in range(100):
        if (worktree / "scratch.txt").exists():
            break
        await asyncio.sleep(0.05)
    work.cancel()
    with pytest.raises(asyncio.CancelledError):
        await work

    # Only Clio Coder's own state (the declared check) stays beside the last commit.
    assert _git(worktree, "status", "--porcelain").splitlines() == ["?? .clio-coder/"]
    assert "a + b" in (worktree / "calc.py").read_text()
    record = load_run(tmp_path / "state", first.run_id)
    assert record.status == "open" and len(record.turns) == 1
    again = await _continue(tmp_path, install, first.run_id, "add @SUB")
    assert again.verified and again.turn == 2


async def test_a_cancelled_first_turn_leaves_no_run_worktree_or_branch(
    repo: Path, tmp_path: Path, install: ClioCoderInstall
) -> None:
    work = asyncio.ensure_future(_open_run(repo, tmp_path, install, "@DIRTY then @HANG"))
    await asyncio.sleep(1.0)
    work.cancel()
    with pytest.raises(asyncio.CancelledError):
        await work
    assert _branches(repo) == ["main"]
    assert not list((tmp_path / "state" / "worktrees").iterdir())
    assert not (tmp_path / "state" / "runs").exists()


async def test_a_crash_or_failed_turn_is_a_failed_turn_the_run_survives(
    repo: Path, tmp_path: Path, install: ClioCoderInstall
) -> None:
    first = await _open_run(repo, tmp_path, install)

    crashed = await _continue(tmp_path, install, first.run_id, "@DIRTY then @CRASH")
    assert crashed.outcome == "failed" and crashed.exit_code == 3
    assert "closed the ACP channel" in crashed.error["message"]
    # What it wrote before dying is on the branch for inspection, but cannot land.
    assert crashed.commit != first.commit and not crashed.verified
    with pytest.raises(ClioCoderError, match="did not succeed"):
        land_run(tmp_path / "state", repo, first.run_id, "apply")

    refused = await _continue(tmp_path, install, first.run_id, "@TURNFAIL")
    assert refused.outcome == "failed" and refused.error["message"].endswith(
        "turn_failed: model no-such-model not found"
    )

    recovered = await _continue(tmp_path, install, first.run_id, "fix add properly")
    assert recovered.outcome == "succeeded" and recovered.verified and recovered.turn == 4
    assert land_run(tmp_path / "state", repo, first.run_id, "apply").status == "landed"


async def test_a_failed_turn_that_changed_nothing_is_not_checked(
    repo: Path, tmp_path: Path, install: ClioCoderInstall
) -> None:
    failed = await _open_run(repo, tmp_path, install, "@TURNFAIL")
    assert failed.outcome == "failed" and not failed.changed_files
    assert failed.verification is None and not failed.verified


async def test_unknown_or_malformed_runs_are_refused_before_any_instance_starts(
    tmp_path: Path, install: ClioCoderInstall
) -> None:
    with pytest.raises(ClioCoderError, match="no Clio Coder run"):
        await _continue(tmp_path, install, "20990101-000000-abcdef", "go")
    with pytest.raises(ClioCoderError, match="not a Clio Coder run id"):
        await _continue(tmp_path, install, "../../etc/passwd", "go")
    with pytest.raises(ClioCoderError, match="not a Clio Coder run id"):
        run_path(tmp_path, "a/b")
