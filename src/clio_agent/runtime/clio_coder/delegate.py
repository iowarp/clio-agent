"""``delegate_coding_task``: spin up a Clio Coder instance, hand it a task, take back the work.

The lifecycle is: resolve the install, prepare a scoped instance home, check out a disposable
worktree, drive Clio Coder over the requested surface, collect the diff, run the caller's own
verification in the worktree, and return one ``DelegationResult``. The worktree and its branch
are kept for the caller to land or discard unless ``keep_worktree`` is false.
"""

from __future__ import annotations

import asyncio
import re
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path

from clio_agent.runtime.clio_coder.acp import PermissionHandler, reject_permissions
from clio_agent.runtime.clio_coder.drivers import EventSink, RunOptions, run_acp, run_headless
from clio_agent.runtime.clio_coder.launch import (
    ClioCoderError,
    ClioCoderInstall,
    ClioCoderTarget,
    InstanceHome,
)
from clio_agent.runtime.clio_coder.results import DelegationResult, command_text
from clio_agent.runtime.clio_coder.workspace import (
    ACCEPTANCE_CHECK_ID,
    Workspace,
    adopt_directory,
    collect_changes,
    commit_changes,
    create_worktree,
    declare_acceptance_check,
    remove_worktree,
    run_verification,
)

SURFACE_HEADLESS = "headless"
SURFACE_ACP = "acp"


@dataclass
class DelegationRequest:
    """What to delegate, where, to which model, and how the parent will check the result.

    ``state_root`` owns everything this delegation writes outside the repository: the instance
    home (``<state_root>/home``) and the worktrees (``<state_root>/worktrees``). It must not be
    the operator's real Clio Coder home.
    """

    task: str
    repo: Path
    state_root: Path
    follow_ups: Sequence[str] = ()
    surface: str | None = None
    isolate: bool = True
    target: ClioCoderTarget | None = None
    home_template: Path | None = None
    #: Variables only the instance process receives, such as a key resolved by the parent.
    child_env: Mapping[str, str] = field(default_factory=dict)
    options: RunOptions = field(default_factory=RunOptions)
    verify_command: Sequence[str] | None = None
    verify_timeout: float = 120.0
    keep_worktree: bool = True
    install: ClioCoderInstall | None = None
    run_id: str | None = None

    def resolved_surface(self) -> str:
        if self.surface is not None:
            if self.surface not in (SURFACE_HEADLESS, SURFACE_ACP):
                raise ValueError(f"unknown Clio Coder surface: {self.surface}")
            return self.surface
        return SURFACE_ACP if self.follow_ups else SURFACE_HEADLESS


def compose_task(
    task: str,
    verify_command: Sequence[str] | None,
    declared: bool,
    *,
    run_after_turn: bool = False,
) -> str:
    """The prompt Clio Coder receives: the task, plus the acceptance check the parent will run."""
    if not verify_command:
        return task
    check = command_text(verify_command)
    if run_after_turn:
        where = "It is also your run's acceptance check; run it yourself after your last edit."
    elif declared:
        where = (
            f"It is declared as the project verifier `{ACCEPTANCE_CHECK_ID}`; you may run it "
            "with the verify tool."
        )
    else:
        where = "You may run it yourself."
    return (
        f"{task}\n\nAcceptance: when you finish, I will run `{check}` in this workspace and "
        f"accept the change only if it exits 0. {where}"
    )


def _path_rebaser(repo: Path, worktree: Path) -> Callable[[str], str]:
    """Map absolute paths inside the operator's checkout onto the same paths in the worktree."""
    pattern = re.compile(re.escape(str(repo)) + r"(?![\w.-])")
    return lambda text: pattern.sub(lambda _match: str(worktree), text)


def _commit_message(task: str) -> str:
    first = next((line.strip() for line in task.splitlines() if line.strip()), "delegated change")
    subject = first if len(first) <= 72 else first[:69].rstrip() + "..."
    return f"clio-coder: {subject}\n\nDelegated by CLIO; the full assignment follows.\n\n{task}"


def _new_run_id() -> str:
    return time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]


async def delegate_coding_task(
    request: DelegationRequest,
    *,
    sink: EventSink | None = None,
    on_permission: PermissionHandler = reject_permissions,
) -> DelegationResult:
    """Run ``request`` on a fresh Clio Coder instance and return its verified hand-back."""
    surface = request.resolved_surface()
    if surface == SURFACE_HEADLESS and request.follow_ups:
        raise ValueError("follow-up prompts need the ACP surface")
    install = request.install or ClioCoderInstall.resolve()
    state_root = request.state_root.resolve()
    home = InstanceHome(state_root / "home", extra_env=dict(request.child_env))
    home.prepare(request.target, template=request.home_template)
    run_id = request.run_id or _new_run_id()

    workspace: Workspace = (
        create_worktree(request.repo, state_root / "worktrees", run_id)
        if request.isolate
        else adopt_directory(request.repo.resolve())
    )
    handed_back = False
    try:
        prompts = [request.task, *request.follow_ups]
        verify_command = list(request.verify_command) if request.verify_command else None
        if workspace.is_worktree and workspace.repo is not None:
            # The instance and the check run in the worktree; checkout paths would escape it.
            rebase = _path_rebaser(workspace.repo, workspace.path)
            prompts = [rebase(prompt) for prompt in prompts]
            verify_command = [rebase(arg) for arg in verify_command] if verify_command else None
        options = request.options
        # A Clio Coder that runs the check itself after its turn (`run --verify`) needs no
        # catalog file in the worktree; older installs and ACP get the declared verifier.
        run_after_turn = (
            surface == SURFACE_HEADLESS
            and bool(verify_command)
            and "--verify" in await asyncio.to_thread(install.help_flags, "run")
        )
        if run_after_turn:
            options = replace(
                options,
                verify=(command_text(verify_command or ()),),
                verify_timeout=request.verify_timeout,
            )
        declared = (
            bool(verify_command)
            and not run_after_turn
            and declare_acceptance_check(workspace, verify_command or (), request.verify_timeout)
        )
        task = compose_task(prompts[0], verify_command, declared, run_after_turn=run_after_turn)
        if surface == SURFACE_ACP:
            result = await run_acp(
                install,
                home,
                workspace.path,
                [task, *prompts[1:]],
                options,
                sink,
                on_permission,
            )
        else:
            result = await run_headless(install, home, workspace.path, task, options, sink)
        result.workspace = str(workspace.path)
        result.branch = workspace.branch
        result.base_commit = workspace.base_commit
        try:
            result.changed_files, result.diff = collect_changes(workspace)
            result.commit = commit_changes(workspace, _commit_message(request.task))
        except ClioCoderError as exc:
            result.error = result.error or exc.to_dict()
        if verify_command:
            result.verification = await run_verification(
                workspace, verify_command, timeout=request.verify_timeout
            )
        handed_back = True
    finally:
        # A run that raised has nothing to hand back, so its worktree is never kept.
        if not (request.keep_worktree and handed_back):
            remove_worktree(workspace)
    return result
