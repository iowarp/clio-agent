"""Disposable git worktrees for delegated work, the diff hand-back, and the caller's own check."""

from __future__ import annotations

import asyncio
import os
import re
import shlex
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import yaml

from clio_agent.runtime.clio_coder.launch import REASON_WORKSPACE, ClioCoderError, close_egress
from clio_agent.runtime.clio_coder.results import Verification, command_text

#: Clio Coder's own project-local state never counts as part of the delegated change.
# Clio Coder's own state and interpreter caches that running code leaves behind are never handed
# back, even in repositories without a .gitignore for them.
_EXCLUDED_PATHSPECS = (
    ":(exclude).clio-coder",
    ":(exclude)CLIO-CODER.md",
    ":(exclude,glob)**/__pycache__/**",
    ":(exclude,glob)**/*.py[co]",
)
_OUTPUT_TAIL_BYTES = 4000
_VERIFIER_CATALOG = Path(".clio-coder") / "verifiers.yaml"
ACCEPTANCE_CHECK_ID = "clio-agent-acceptance"
# Clio Coder's verifier catalog runs a command vector and refuses these as the executable.
_SHELL_EXECUTABLES = frozenset(
    {"bash", "cmd", "command.com", "dash", "fish", "ksh", "powershell", "pwsh", "sh", "tcsh", "zsh"}
)
_SHELL_SYNTAX = re.compile(r"[|&;<>()$`*?\[\]{}~!#\n\\]")


@dataclass(frozen=True)
class Workspace:
    path: Path
    repo: Path | None = None
    branch: str | None = None
    base_commit: str | None = None

    @property
    def is_worktree(self) -> bool:
        return self.branch is not None


def _git(cwd: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        check=False,
        stdin=subprocess.DEVNULL,
    )
    if completed.returncode != 0:
        raise ClioCoderError(
            REASON_WORKSPACE,
            f"git {args[0]} failed",
            {"args": list(args), "stderr": completed.stderr.strip()[-2000:]},
        )
    return completed.stdout


def create_worktree(repo: Path, root: Path, run_id: str) -> Workspace:
    """Check out ``repo``'s HEAD into ``root/run_id`` on a fresh ``clio-coder/<run_id>`` branch."""
    repo = Path(_git(repo, "rev-parse", "--show-toplevel").strip())
    base = _git(repo, "rev-parse", "HEAD").strip()
    branch = f"clio-coder/{run_id}"
    path = root / run_id
    root.mkdir(parents=True, exist_ok=True)
    _git(repo, "worktree", "add", "--quiet", "-b", branch, str(path), base)
    return Workspace(path=path, repo=repo, branch=branch, base_commit=base)


def adopt_directory(path: Path) -> Workspace:
    """Use an existing directory in place; the base is its HEAD when it is a git checkout."""
    try:
        base = _git(path, "rev-parse", "HEAD").strip()
    except ClioCoderError:
        base = None
    return Workspace(path=path, base_commit=base)


def collect_changes(workspace: Workspace) -> tuple[list[str], str]:
    """Return the changed paths and the unified diff against the base, untracked files included."""
    if workspace.base_commit is None:
        return [], ""
    if workspace.is_worktree:
        _git(workspace.path, "add", "--all", "--", ".", *_EXCLUDED_PATHSPECS)
        diff_args = ("diff", "--cached", workspace.base_commit, "--", ".", *_EXCLUDED_PATHSPECS)
    else:
        # In an adopted checkout the index belongs to the operator: report without staging.
        diff_args = ("diff", workspace.base_commit, "--", ".", *_EXCLUDED_PATHSPECS)
    names = _git(workspace.path, *diff_args[:1], "--name-only", *diff_args[1:])
    return [line for line in names.splitlines() if line], _git(workspace.path, *diff_args)


def commit_changes(workspace: Workspace, message: str) -> str | None:
    """Commit the change staged by :func:`collect_changes` onto the worktree's branch.

    The branch is the hand-back the parent lands (merge, cherry-pick or ``git show``), so it must
    carry the change rather than the base. Runs before the acceptance check so the check's own
    artifacts never enter the commit. Returns the commit id, or ``None`` when nothing changed.
    """
    if not workspace.is_worktree:
        return None
    if not _git(workspace.path, "diff", "--cached", "--name-only"):
        return None
    _git(
        workspace.path,
        "-c",
        "user.name=Clio Coder",
        "-c",
        "user.email=clio-coder@delegated.invalid",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "--quiet",
        "--no-verify",
        "-m",
        message,
    )
    return _git(workspace.path, "rev-parse", "HEAD").strip()


def catalog_argv(command: Sequence[str]) -> list[str] | None:
    """The check as a shell-free argv Clio Coder's verifier catalog accepts, or None.

    The catalog runs a command vector and refuses shell executables, so a check written as shell
    text (``/bin/sh -c "..."``) is declarable only when that text is a plain command with no
    shell syntax; otherwise it stays CLIO's own check and is not declared.
    """
    argv = list(command)
    text = command_text(argv)
    if text != shlex.join(argv):
        if _SHELL_SYNTAX.search(text):
            return None
        try:
            argv = shlex.split(text)
        except ValueError:
            return None
    if not argv or "=" in argv[0] or Path(argv[0]).name in _SHELL_EXECUTABLES:
        return None
    return argv


def declare_acceptance_check(workspace: Workspace, command: Sequence[str], timeout: float) -> bool:
    """Declare the parent's acceptance check as a Clio Coder verifier in the worktree.

    Clio Coder's finish gate then validates against the same check the parent will run, instead
    of recording an unverifiable limitation. Only a worktree is touched; an adopted checkout
    belongs to the operator. A check that is not a shell-free command is not declared, and a
    previous declaration is withdrawn. Returns whether the check is declared.
    """
    if not workspace.is_worktree:
        return False
    argv = catalog_argv(command)
    catalog_path = workspace.path / _VERIFIER_CATALOG
    if argv is None and not catalog_path.exists():
        return False
    catalog: dict = {"version": 2, "checks": []}
    if catalog_path.exists():
        loaded = yaml.safe_load(catalog_path.read_text("utf-8")) or {}
        if not isinstance(loaded, dict) or not isinstance(loaded.get("checks", []), list):
            raise ClioCoderError(
                REASON_WORKSPACE,
                "the repository's .clio-coder/verifiers.yaml is not a check catalog",
                {"path": str(catalog_path)},
            )
        catalog = {**loaded, "checks": list(loaded.get("checks", []))}
    catalog["checks"] = [c for c in catalog["checks"] if c.get("id") != ACCEPTANCE_CHECK_ID]
    if argv is None:
        catalog_path.write_text(yaml.safe_dump(catalog, sort_keys=False), "utf-8")
        return False
    catalog["checks"].append(
        {
            "id": ACCEPTANCE_CHECK_ID,
            "description": "Acceptance check the delegating agent runs on the hand-back",
            "command": argv,
            "cwd": ".",
            "timeoutMs": int(timeout * 1000),
            "tags": ["test", "acceptance"],
        }
    )
    catalog_path.parent.mkdir(parents=True, exist_ok=True)
    catalog_path.write_text(yaml.safe_dump(catalog, sort_keys=False), "utf-8")
    return True


def restore_worktree(workspace: Workspace, commit: str = "HEAD") -> None:
    """Reset the worktree branch to ``commit`` and drop the rest, keeping Clio Coder's state."""
    if not workspace.is_worktree:
        return
    _git(workspace.path, "reset", "--quiet", "--hard", commit)
    _git(workspace.path, "clean", "-fdq", "-e", ".clio-coder", "-e", "CLIO-CODER.md")


def remove_worktree(workspace: Workspace) -> None:
    """Delete the worktree and the branch this module created for it."""
    if not workspace.is_worktree or workspace.repo is None:
        return
    _git(workspace.repo, "worktree", "remove", "--force", str(workspace.path))
    _git(workspace.repo, "branch", "-D", str(workspace.branch))


async def run_verification(
    workspace: Workspace, command: Sequence[str], *, timeout: float
) -> Verification:
    """Run the caller's check in the workspace as an owned, confined process tree."""
    from clio_agent.runtime import sandbox
    from clio_agent.runtime.task_process_tree import TaskProcessTree, task_spawn_options

    try:
        confined = sandbox.wrap_confined(
            command[0],
            list(command[1:]),
            write_roots=(workspace.path,),
            profile=sandbox.PROFILE_SHELL,
            cwd=workspace.path,
        )
    except sandbox.SandboxCompositionError as exc:
        raise ClioCoderError(
            "clio_coder_check_failed", f"verification confinement failed: {exc}"
        ) from exc
    try:
        process = await asyncio.create_subprocess_exec(
            confined.command,
            *confined.args,
            cwd=str(workspace.path),
            env={**os.environ, **confined.env_overlay},
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            **task_spawn_options(confined.popen_kwargs),
        )
    except BaseException:
        close_egress(confined.result.details)
        raise
    try:
        tree = TaskProcessTree(process.pid)
    except BaseException:
        close_egress(confined.result.details)
        raise
    try:
        output, _ = await asyncio.wait_for(process.communicate(), timeout)
        exit_code = process.returncode
    except TimeoutError:
        tree.terminate()
        await process.wait()
        output, exit_code = f"verification timed out after {timeout}s".encode(), None
    finally:
        if tree.active():
            tree.terminate()
        await process.wait()
        tree.close()
        close_egress(confined.result.details)
    text = output.decode("utf-8", "replace")
    return Verification(list(command), exit_code, text[-_OUTPUT_TAIL_BYTES:])
