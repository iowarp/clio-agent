"""Interactive Clio Coder sessions in a terminal the person can see.

The person works with Clio Coder's own terminal UI while CLIO keeps the session's boundaries:
the TUI starts in a disposable worktree with a scoped home and the same OS fence as a headless
delegation. CLIO only describes the launch; the terminal host (the Desktop terminal, a Herdr
tab, any PTY) runs it. When the person is done, :func:`finish_interactive_session` hands the
work back exactly like a headless delegation: committed on the worktree branch and checked by
the caller's acceptance command.
"""

from __future__ import annotations

import os
import shlex
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from clio_agent.runtime.clio_coder.delegate import (
    DelegationRequest,
    _commit_message,
    _new_run_id,
    _path_rebaser,
)
from clio_agent.runtime.clio_coder.launch import (
    ClioCoderError,
    ClioCoderInstall,
    InstanceHome,
    close_egress,
    confine,
)
from clio_agent.runtime.clio_coder.results import DelegationResult
from clio_agent.runtime.clio_coder.workspace import (
    Workspace,
    collect_changes,
    commit_changes,
    create_worktree,
    declare_acceptance_check,
    remove_worktree,
    run_verification,
)

SURFACE_TUI = "tui"


@dataclass(frozen=True)
class TerminalLaunch:
    """What a terminal host runs: argv in ``cwd`` with exactly ``env``."""

    argv: tuple[str, ...]
    cwd: Path
    env: Mapping[str, str]

    def env_changes(
        self, base: Mapping[str, str] | None = None
    ) -> tuple[dict[str, str], list[str]]:
        """The variables to set and to unset relative to ``base`` (the host's environment)."""
        base = os.environ if base is None else base
        assign = {key: value for key, value in self.env.items() if base.get(key) != value}
        unset = sorted(key for key in base if key not in self.env)
        return assign, unset

    def shell_command(
        self, base: Mapping[str, str] | None = None, *, exit_file: Path | None = None
    ) -> str:
        """A POSIX command line for hosts that can only type into a shell.

        With ``exit_file`` the TUI's exit status is written there when it ends, so a caller
        that cannot see the terminal knows when to hand the work back.
        """
        assign, unset = self.env_changes(base)
        words = ["env", *(f"-u{key}" for key in unset)]
        words += [f"{key}={value}" for key, value in sorted(assign.items())]
        run = shlex.join([*words, *self.argv])
        cd = f"cd {shlex.quote(str(self.cwd))}"
        if exit_file is None:
            return f"{cd} && exec {run}"
        return f"{cd} && {run}; echo $? > {shlex.quote(str(exit_file))}"


@dataclass
class InteractiveSession:
    """A prepared TUI session and what is needed to hand its work back."""

    run_id: str
    task: str
    workspace: Workspace
    launch: TerminalLaunch
    verify_command: list[str] | None
    verify_timeout: float
    confinement: dict[str, Any] = field(default_factory=dict)

    def brief(self) -> str:
        """The assignment the person (or the agent beside them) starts the session with."""
        lines = [self.task.strip()] if self.task.strip() else []
        if self.verify_command:
            lines.append(f"Acceptance: `{shlex.join(self.verify_command)}` must exit 0.")
        return "\n\n".join(lines)


def prepare_interactive_session(
    request: DelegationRequest, *, autonomy: str | None = None
) -> InteractiveSession:
    """Create the worktree and scoped home, and describe the fenced TUI launch."""
    install = request.install or ClioCoderInstall.resolve()
    state_root = request.state_root.resolve()
    home = InstanceHome(state_root / "home")
    home.prepare(request.target, template=request.home_template)
    run_id = request.run_id or _new_run_id()
    workspace = create_worktree(request.repo, state_root / "worktrees", run_id)
    try:
        rebase = _path_rebaser(workspace.repo or request.repo.resolve(), workspace.path)
        task = rebase(request.task)
        verify = [rebase(arg) for arg in request.verify_command or ()] or None
        if verify:
            declare_acceptance_check(workspace, verify, request.verify_timeout)
        args = ["--autonomy", autonomy] if autonomy else []
        argv, overlay, confinement = confine(install, args, cwd=workspace.path, home=home)
    except BaseException:
        remove_worktree(workspace)
        raise
    env = {**home.environment(), **overlay}
    return InteractiveSession(
        run_id=run_id,
        task=task,
        workspace=workspace,
        launch=TerminalLaunch(argv=tuple(argv), cwd=workspace.path, env=env),
        verify_command=verify,
        verify_timeout=request.verify_timeout,
        confinement=confinement,
    )


async def finish_interactive_session(
    session: InteractiveSession, *, keep_worktree: bool = True, exit_code: int | None = 0
) -> DelegationResult:
    """Hand back what the person and Clio Coder changed; call once the TUI has exited."""
    close_egress(session.confinement)
    workspace = session.workspace
    result = DelegationResult(
        surface=SURFACE_TUI,
        outcome="succeeded" if exit_code == 0 else "failed",
        exit_code=exit_code,
        workspace=str(workspace.path),
        branch=workspace.branch,
        base_commit=workspace.base_commit,
        confinement=dict(session.confinement),
    )
    try:
        result.changed_files, result.diff = collect_changes(workspace)
        result.commit = commit_changes(workspace, _commit_message(session.task or "TUI session"))
    except ClioCoderError as exc:
        result.error = exc.to_dict()
    if session.verify_command:
        result.verification = await run_verification(
            workspace, session.verify_command, timeout=session.verify_timeout
        )
    if not keep_worktree:
        remove_worktree(workspace)
    return result


def describe_launch(session: InteractiveSession, base: Mapping[str, str] | None = None) -> dict:
    """A JSON-safe record of the launch, with the environment reduced to its changes."""
    assign, unset = session.launch.env_changes(base)
    secret = ("KEY", "TOKEN", "SECRET", "PASSWORD")
    return {
        "run_id": session.run_id,
        "argv": list(session.launch.argv),
        "cwd": str(session.launch.cwd),
        "env_set": {k: ("<set>" if any(s in k for s in secret) else v) for k, v in assign.items()},
        "env_unset": unset,
        "branch": session.workspace.branch,
        "base_commit": session.workspace.base_commit,
        "confinement": {k: v for k, v in session.confinement.items() if k != "net_child_id"},
        "brief": session.brief(),
    }


__all__: Sequence[str] = (
    "SURFACE_TUI",
    "InteractiveSession",
    "TerminalLaunch",
    "describe_launch",
    "finish_interactive_session",
    "prepare_interactive_session",
)
