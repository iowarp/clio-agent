"""Land or discard a Clio Coder run in the operator's checkout, from the CLIO side.

The agent's own shell is fenced and sees the checkout's ``.git`` read-only, and while the
``coding`` plugin is on the agent does not edit source itself. Landing is therefore CLIO's
step, not the agent's: it applies exactly the run's verified commits (``base..tip``) to the
checkout's working tree, and only when the run's last turn succeeded and CLIO's acceptance
check passed on that tip. The person commits the applied change; nothing is committed or
merged on their branch.
"""

from __future__ import annotations

import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from clio_agent.runtime.clio_coder.launch import ClioCoderError
from clio_agent.runtime.clio_coder.runs import (
    REASON_RUN,
    RUN_DISCARDED,
    RUN_LANDED,
    RUN_OPEN,
    RunRecord,
    load_run,
    run_lock,
    run_path,
    save_run,
)

ACTION_APPLY = "apply"
ACTION_DISCARD = "discard"
REASON_LAND = "clio_coder_land"


@dataclass
class LandResult:
    run_id: str
    action: str
    status: str
    checkout: str
    branch: str
    base_commit: str
    commit: str | None
    changed_files: list[str] = field(default_factory=list)
    diff: str = ""
    removed_worktree: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _git(cwd: Path | str, *args: str, stdin: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        input=stdin,
        capture_output=True,
        text=True,
        check=False,
        **({} if stdin is not None else {"stdin": subprocess.DEVNULL}),
    )


def _out(cwd: Path | str, *args: str) -> str:
    done = _git(cwd, *args)
    if done.returncode != 0:
        raise ClioCoderError(
            REASON_LAND, f"git {args[0]} failed", {"stderr": done.stderr.strip()[-2000:]}
        )
    return done.stdout


def _leftovers(record: RunRecord) -> bool:
    branch = _git(record.repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{record.branch}")
    return Path(record.worktree).exists() or branch.returncode == 0


def retire_worktree(record: RunRecord) -> bool:
    """Remove the run's worktree and branch; ``True`` once neither is left."""
    # Twice forced: a locked worktree refuses a single --force.
    _git(record.repo, "worktree", "remove", "--force", "--force", record.worktree)
    _git(record.repo, "worktree", "prune")
    _git(record.repo, "branch", "-D", record.branch)
    return not _leftovers(record)


def _checkout_root(repo: Path) -> str:
    return _out(repo, "rev-parse", "--show-toplevel").strip()


def land_run(state_root: Path, repo: Path, run_id: str, action: str) -> LandResult:
    """Apply an accepted run to ``repo``'s working tree, or discard it; either ends the run."""
    if action not in (ACTION_APPLY, ACTION_DISCARD):
        raise ValueError(f"action must be {ACTION_APPLY} or {ACTION_DISCARD}")
    with run_lock(state_root, run_id):
        result = _land_locked(state_root, repo, run_id, action)
        # The run has ended and refuses every later turn or land, so its lock file goes too.
        run_path(state_root, run_id).with_suffix(".lock").unlink(missing_ok=True)
        return result


def _land_locked(state_root: Path, repo: Path, run_id: str, action: str) -> LandResult:
    record = load_run(state_root, run_id)
    checkout = _checkout_root(repo)
    if Path(record.repo).resolve() != Path(checkout).resolve():
        raise ClioCoderError(REASON_RUN, f"run {run_id} belongs to another checkout")
    result = LandResult(
        run_id=run_id,
        action=action,
        status=record.status,
        checkout=checkout,
        branch=record.branch,
        base_commit=record.base_commit,
        commit=record.tip,
    )
    if action == ACTION_DISCARD:
        if record.status != RUN_OPEN:
            # An ended run whose cleanup failed is retired again; otherwise there is nothing to do.
            if not _leftovers(record):
                raise ClioCoderError(REASON_RUN, f"run {run_id} is already {record.status}")
            result.removed_worktree = retire_worktree(record)
            return result
        result.removed_worktree = retire_worktree(record)
        record.status = result.status = RUN_DISCARDED
        save_run(state_root, record)
        return result

    if gap := record.acceptance_gap():
        raise ClioCoderError(REASON_LAND, f"run {run_id} cannot land: {gap}", {"run": run_id})
    tip = record.tip or ""
    branch_head = _git(checkout, "rev-parse", "--verify", "--quiet", record.branch).stdout.strip()
    if branch_head != tip:
        raise ClioCoderError(
            REASON_LAND,
            f"run {run_id} cannot land: its branch moved after the accepted commit",
            {"branch": record.branch, "head": branch_head, "accepted": tip},
        )
    files = [
        f for f in _out(checkout, "diff", "--name-only", record.base_commit, tip).split("\n") if f
    ]
    if not files:
        raise ClioCoderError(
            REASON_LAND, f"run {run_id} has no change to land; discard it", {"run": run_id}
        )
    dirty = [f for f in _out(checkout, "status", "--porcelain", "--", *files).splitlines() if f]
    if dirty:
        raise ClioCoderError(
            REASON_LAND,
            f"run {run_id} cannot land: the checkout has uncommitted changes in files it touches",
            {"files": [line[3:] for line in dirty]},
        )
    patch = _out(checkout, "diff", "--binary", record.base_commit, tip)
    check = _git(checkout, "apply", "--check", "-", stdin=patch)
    if check.returncode != 0:
        raise ClioCoderError(
            REASON_LAND,
            f"run {run_id} does not apply to the checkout as it is now",
            {"stderr": check.stderr.strip()[-2000:]},
        )
    applied = _git(checkout, "apply", "-", stdin=patch)
    if applied.returncode != 0:
        raise ClioCoderError(
            REASON_LAND, "git apply failed", {"stderr": applied.stderr.strip()[-2000:]}
        )
    result.changed_files, result.diff = files, _out(checkout, "diff", record.base_commit, tip)
    result.removed_worktree = retire_worktree(record)
    record.status = result.status = RUN_LANDED
    record.landed_into = checkout
    save_run(state_root, record)
    return result
