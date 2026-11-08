"""Arena matches and Research campaigns: Clio Coder's multi-run coding surfaces, delegated.

``clio-coder arena run`` runs one prompt on 2 to 4 contenders under blind labels and records a
person's vote; ``clio-coder research start`` hill climbs one metric of a declared check within a
budget and ratchets kept changes onto ``clio/research/<id>``. Both start their own nested Clio
Coder runs and git worktrees, so they need a repository whose ``.git`` they can write. Under the
write fence a linked worktree's ``.git`` lives in the operator's checkout, which stays read-only.

Each campaign therefore runs in a scratch clone of the checkout's HEAD (``git clone --shared``:
objects are read from the checkout, new ones land in the clone) one level below its own writable
territory, because the fence also keeps ``.git`` read-only at the top of each writable root. The
child owns that clone's ``.git`` (hooks and config included), so the parent never runs a git
command in it that could execute them: results come back by ``git fetch`` from the checkout
side, and an adopted arena entry is applied in a fresh parent-made worktree of the checkout. The person's working tree and
current branch are never touched. Clio Coder records and never judges; neither does this module:
an arena vote is the person's call, passed through ``vote_arena``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import shutil
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from clio_agent.runtime.clio_coder.drivers import EventSink
from clio_agent.runtime.clio_coder.launch import (
    REASON_PROTOCOL,
    REASON_WORKSPACE,
    ClioCoderError,
    ClioCoderInstall,
    InstanceHome,
    spawn_instance,
)
from clio_agent.runtime.clio_coder.workspace import (
    _git,
    commit_changes,
    create_worktree,
    remove_worktree,
)

#: Interpreter caches the checks leave behind; research would otherwise count them as changes.
_CLONE_EXCLUDES = ("__pycache__/", "*.py[co]")
_VERIFIER_CATALOG = Path(".clio-coder") / "verifiers.yaml"
_STDERR_LINES = 200
_WATCHDOG_SLACK_SECONDS = 60.0
ARENA_CHOICES = ("A", "B", "C", "D", "tie", "both-bad")


@dataclass(frozen=True)
class CampaignWorkspace:
    """A scratch clone of ``repo`` at ``base_commit`` that one arena match or campaign owns.

    ``territory`` is the campaign's writable root; the clone is ``territory / "repo"``.
    """

    repo: Path
    territory: Path
    base_commit: str

    @property
    def clone(self) -> Path:
        return self.territory / "repo"


def prepare_campaign_clone(repo: Path, root: Path, run_id: str) -> CampaignWorkspace:
    """Clone ``repo``'s HEAD into ``root/run_id``; carry an uncommitted check catalog along."""
    repo = repo.resolve()
    base = _git(repo, "rev-parse", "HEAD").strip()
    workspace = CampaignWorkspace(repo=repo, territory=root.resolve() / run_id, base_commit=base)
    clone = workspace.clone
    if workspace.territory.exists():
        raise ClioCoderError(
            REASON_WORKSPACE, "campaign directory already exists", {"path": str(clone)}
        )
    workspace.territory.mkdir(parents=True)
    _git(
        workspace.territory, "clone", "--quiet", "--shared", "--no-checkout", str(repo), str(clone)
    )
    _git(clone, "checkout", "--quiet", "-B", "clio-agent/base", base)
    with (clone / ".git" / "info" / "exclude").open("a", encoding="utf-8") as handle:
        handle.write("".join(f"{pattern}\n" for pattern in _CLONE_EXCLUDES))
    catalog = repo / _VERIFIER_CATALOG
    if catalog.is_file():
        # Checks the person declared but did not commit still define the objective and gates.
        (clone / _VERIFIER_CATALOG).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(catalog, clone / _VERIFIER_CATALOG)
    return workspace


def discard_campaign(workspace: CampaignWorkspace, root: Path) -> None:
    """Delete the scratch clone; refuses any path outside the caller's state root."""
    root = root.resolve()
    territory = workspace.territory.resolve()
    if root not in territory.parents:
        raise ClioCoderError(
            REASON_WORKSPACE, "refusing to delete outside the state root", {"path": str(territory)}
        )
    shutil.rmtree(territory, ignore_errors=True)


def fetch_into_checkout(workspace: CampaignWorkspace, source_ref: str, branch: str) -> str:
    """Fetch ``source_ref`` from the clone into the checkout as ``branch``; returns its commit.

    Fetching from a local path runs ``upload-pack`` in the clone, which ignores repository-level
    hook settings, so the child-owned ``.git`` cannot run code in the parent this way.
    """
    _git(
        workspace.repo,
        "fetch",
        "--quiet",
        str(workspace.clone),
        f"+{source_ref}:refs/heads/{branch}",
    )
    return _git(workspace.repo, "rev-parse", f"refs/heads/{branch}").strip()


@dataclass
class CliRun:
    exit_code: int | None
    stdout: str
    progress: list[str] = field(default_factory=list)
    timed_out: bool = False
    confinement: dict[str, Any] = field(default_factory=dict)

    def payload(self, what: str) -> dict[str, Any]:
        try:
            loaded = json.loads(self.stdout)
        except json.JSONDecodeError as exc:
            raise ClioCoderError(
                REASON_PROTOCOL,
                f"{what} printed no JSON",
                {
                    "exit_code": self.exit_code,
                    "progress": self.progress[-10:],
                    "stdout": self.stdout[-1000:],
                },
            ) from exc
        if not isinstance(loaded, dict):
            raise ClioCoderError(REASON_PROTOCOL, f"{what} printed a non-object", {})
        return loaded


async def run_cli(
    install: ClioCoderInstall,
    home: InstanceHome,
    workspace: CampaignWorkspace,
    args: Sequence[str],
    *,
    timeout: float,
    sink: EventSink | None = None,
) -> CliRun:
    """Run one fenced Clio Coder command to completion; stderr lines stream as progress events."""
    instance = await spawn_instance(
        install,
        args,
        cwd=workspace.clone,
        home=home,
        stdin_pipe=False,
        territory=workspace.territory,
    )
    run = CliRun(exit_code=None, stdout="", confinement=instance.confinement)

    async def progress() -> None:
        stream = instance.process.stderr
        while stream is not None and (line := await stream.readline()):
            text = line.decode("utf-8", "replace").rstrip()
            run.progress = [*run.progress[-(_STDERR_LINES - 1) :], text]
            if sink is not None:
                sink({"kind": "progress", "text": text})

    progress_task = asyncio.create_task(progress())
    try:
        assert instance.process.stdout is not None
        data = await asyncio.wait_for(instance.process.stdout.read(), timeout)
        run.stdout = data.decode("utf-8", "replace")
        await asyncio.wait_for(instance.process.wait(), _WATCHDOG_SLACK_SECONDS)
    except TimeoutError:
        run.timed_out = True
    finally:
        # SIGTERM first: research journals the stop and removes its worktrees before exiting.
        run.exit_code = await instance.stop(grace=_WATCHDOG_SLACK_SECONDS)
        with contextlib.suppress(asyncio.CancelledError, TimeoutError):
            await asyncio.wait_for(progress_task, 5.0)
    return run


# --- Arena --------------------------------------------------------------------------------


@dataclass(frozen=True)
class ArenaRequest:
    prompt: str
    contenders: Sequence[str]
    check: str | None = None
    timeout: float = 600.0
    max_cost_usd: float | None = None
    suite: str | None = None
    seed: int | None = None

    def args(self) -> list[str]:
        if not 2 <= len(self.contenders) <= 4:
            raise ValueError("an arena match needs 2 to 4 contenders")
        for contender in self.contenders:
            if "/" not in contender:
                raise ValueError(f"contender must be <target>/<model>: {contender}")
        args = ["arena", "run", "--json", "--timeout", str(max(1, int(self.timeout)))]
        for contender in self.contenders:
            args += ["--contender", contender]
        if self.check:
            args += ["--check", self.check]
        if self.max_cost_usd is not None:
            args += ["--max-cost-usd", str(self.max_cost_usd)]
        if self.suite:
            args += ["--suite", self.suite]
        if self.seed is not None:
            args += ["--seed", str(self.seed)]
        return [*args, self.prompt]


def arena_entry_summary(entry: Mapping[str, Any]) -> dict[str, Any]:
    """The blind view of one contender: never its identity, duration, tokens or cost."""
    check = entry.get("check") or None
    return {
        "label": entry.get("label"),
        "status": entry.get("status"),
        "exit_code": entry.get("exitCode"),
        "timed_out": bool(entry.get("timedOut")),
        "tool_calls": entry.get("toolCalls"),
        "tool_errors": entry.get("toolErrors"),
        "diff_stat": entry.get("diffStat") or {},
        "check": None
        if check is None
        else {"id": check.get("id"), "passed": check.get("exitCode") == 0},
        "final_text": str(entry.get("finalText") or ""),
        "diff": str(entry.get("diff") or ""),
        "diff_truncated": bool(entry.get("diffTruncated")),
        "error": entry.get("error"),
    }


async def run_arena(
    install: ClioCoderInstall,
    home: InstanceHome,
    workspace: CampaignWorkspace,
    request: ArenaRequest,
    *,
    sink: EventSink | None = None,
) -> dict[str, Any]:
    """Run the match in the clone and return its blind view (``match.entries`` summarized)."""
    # Contenders run concurrently, each bounded by --timeout; the parent allows setup on top.
    run = await run_cli(
        install,
        home,
        workspace,
        request.args(),
        timeout=request.timeout * 1.5 + 120,
        sink=sink,
    )
    if run.timed_out:
        raise ClioCoderError(
            REASON_PROTOCOL,
            "arena match exceeded the parent watchdog",
            {"progress": run.progress[-10:]},
        )
    match = run.payload("arena run")
    return {
        "match_id": match.get("id"),
        "status": match.get("status"),
        "error": match.get("error"),
        "check": match.get("check"),
        "isolation": match.get("isolation"),
        "entries": [arena_entry_summary(entry) for entry in match.get("entries") or []],
        "confinement": run.confinement,
    }


async def vote_arena(
    install: ClioCoderInstall,
    home: InstanceHome,
    workspace: CampaignWorkspace,
    match_id: str,
    choice: str,
    *,
    note: str | None = None,
) -> dict[str, Any]:
    """Record the person's vote, then return the revealed match (identities, cost, tokens)."""
    if choice not in ARENA_CHOICES:
        raise ValueError(f"vote must be one of {', '.join(ARENA_CHOICES)}")
    args = ["arena", "vote", match_id, choice, "--json"]
    if note:
        args += ["--note", note]
    voted = await run_cli(install, home, workspace, args, timeout=60)
    if voted.exit_code != 0:
        raise ClioCoderError(
            REASON_PROTOCOL,
            "arena vote was refused",
            {"progress": voted.progress[-5:], "stdout": voted.stdout[-500:]},
        )
    shown = await run_cli(
        install,
        home,
        workspace,
        ["arena", "show", match_id, "--reveal", "--json"],
        timeout=60,
    )
    return shown.payload("arena show --reveal")


def adopt_arena_entry(
    workspace: CampaignWorkspace, match: Mapping[str, Any], label: str, worktree_root: Path
) -> dict[str, Any]:
    """Commit contender ``label``'s diff onto the base as branch ``clio-coder/arena-<id>-<label>``.

    The diff is applied in a fresh worktree of the checkout made by the parent, never in the
    child-owned clone, and the worktree is removed afterwards; the branch stays.
    """
    entry = next((e for e in match.get("entries") or [] if e.get("label") == label), None)
    if entry is None:
        raise ClioCoderError(REASON_WORKSPACE, f"no contender {label} in this match", {})
    if entry.get("diff_truncated"):
        raise ClioCoderError(
            REASON_WORKSPACE, "the contender's diff was truncated in the match view", {}
        )
    diff = str(entry.get("diff") or "")
    if not diff.strip():
        raise ClioCoderError(REASON_WORKSPACE, f"contender {label} changed nothing", {})
    match_id = str(match.get("match_id"))
    target = create_worktree(workspace.repo, worktree_root, f"arena-{match_id}-{label}")
    try:
        if target.base_commit != workspace.base_commit:
            _git(
                target.path, "checkout", "--quiet", "-B", str(target.branch), workspace.base_commit
            )
        applied = subprocess.run(
            ["git", "-C", str(target.path), "apply", "--index", "-"],
            input=diff,
            capture_output=True,
            text=True,
            check=False,
        )
        if applied.returncode != 0:
            raise ClioCoderError(
                REASON_WORKSPACE,
                "the contender's diff does not apply",
                {"stderr": applied.stderr[-1000:]},
            )
        commit = commit_changes(target, f"clio-coder arena {match_id}: contender {label}")
    except BaseException:
        remove_worktree(target)
        raise
    _git(workspace.repo, "worktree", "remove", "--force", str(target.path))
    return {"branch": target.branch, "commit": commit}


# --- Research -----------------------------------------------------------------------------


@dataclass(frozen=True)
class ResearchRequest:
    check: str
    metric: str
    direction: str
    max_iterations: int
    gates: Sequence[str] = ()
    scope: Sequence[str] = ()
    goal: str | None = None
    trials: int | None = None
    min_improvement: float | None = None
    max_wall_minutes: float = 30.0
    max_cost_usd: float | None = None
    allow_unpriced: bool = False
    target: str | None = None
    model: str | None = None

    def args(self) -> list[str]:
        if self.direction not in ("min", "max"):
            raise ValueError("direction must be min or max")
        if self.max_iterations < 1:
            raise ValueError("max_iterations must be at least 1")
        args = ["research", "start", "--json", "--check", self.check, "--metric", self.metric]
        args += ["--direction", self.direction, "--max-iterations", str(self.max_iterations)]
        # A wall deadline always binds, so the parent's watchdog never has to cut a campaign.
        args += ["--max-wall-minutes", str(self.max_wall_minutes)]
        for gate in self.gates:
            args += ["--gate", gate]
        for pattern in self.scope:
            args += ["--scope", pattern]
        if self.goal:
            args += ["--goal", self.goal]
        if self.trials is not None:
            args += ["--trials", str(self.trials)]
        if self.min_improvement is not None:
            args += ["--min-improvement", str(self.min_improvement)]
        if self.max_cost_usd is not None:
            args += ["--max-cost-usd", str(self.max_cost_usd)]
        if self.allow_unpriced:
            args.append("--allow-unpriced")
        if self.target:
            args += ["--target", self.target]
        if self.model:
            args += ["--model", self.model]
        return args


def _iteration_summary(iteration: Mapping[str, Any]) -> dict[str, Any]:
    grade = iteration.get("grade") or {}
    return {
        "iteration": iteration.get("iteration"),
        "outcome": iteration.get("outcome"),
        "detail": iteration.get("detail"),
        "changed": list(iteration.get("changed") or []),
        "base_median": grade.get("baseMedian"),
        "candidate_median": grade.get("candidateMedian"),
        "verdict": grade.get("verdict"),
        "gates": [
            {"check": g.get("check"), "passed": g.get("passed")}
            for g in iteration.get("gates") or []
        ],
        "hypothesis": iteration.get("hypothesis"),
    }


async def run_research(
    install: ClioCoderInstall,
    home: InstanceHome,
    workspace: CampaignWorkspace,
    request: ResearchRequest,
    *,
    sink: EventSink | None = None,
) -> dict[str, Any]:
    """Run the campaign in the clone; a moved best commit comes back as a checkout branch."""
    run = await run_cli(
        install,
        home,
        workspace,
        request.args(),
        timeout=request.max_wall_minutes * 60 + 120,
        sink=sink,
    )
    status = run.payload("research start")
    campaign_id = str(status.get("id") or "")
    best = str(status.get("best") or "")
    iterations = [_iteration_summary(it) for it in status.get("iterations") or []]
    kept = [it for it in iterations if it["outcome"] == "ratcheted"]
    result: dict[str, Any] = {
        "campaign_id": campaign_id,
        "exit_code": run.exit_code,
        "state": status.get("state"),
        "stop": status.get("stop"),
        "ratchets": status.get("ratchets"),
        "spend": status.get("spend"),
        "aa": (status.get("aa") or {}).get("grade"),
        "base_commit": workspace.base_commit,
        "best_commit": best,
        "best_metric": kept[-1]["candidate_median"] if kept else None,
        "iterations": iterations,
        "branch": None,
        "commit": None,
        "confinement": run.confinement,
    }
    if campaign_id and best and best != workspace.base_commit:
        branch = f"clio-coder/research-{campaign_id}"
        result["branch"] = branch
        result["commit"] = fetch_into_checkout(
            workspace, f"refs/heads/clio/research/{campaign_id}", branch
        )
    return result
