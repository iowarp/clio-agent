"""``delegate_coding_task``: spin up a Clio Coder instance, hand it a task, take back the work.

The lifecycle is: resolve the install, prepare a scoped instance home, check out a disposable
worktree, drive Clio Coder over the requested surface, collect the diff, run the caller's own
verification in the worktree, and return one ``DelegationResult``. The worktree and its branch
are kept for the caller to land or discard unless ``keep_worktree`` is false.

An ACP delegation in a worktree can be a resumable *run* (``resumable``): its record keeps the
session, branch and check, and :func:`continue_run` gives it further turns from a later call,
each committed and checked like the first.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path

from clio_agent.runtime.clio_coder.acp import PermissionHandler, reject_permissions
from clio_agent.runtime.clio_coder.drivers import (
    EventSink,
    RunOptions,
    add_usage,
    run_acp,
    run_headless,
)
from clio_agent.runtime.clio_coder.launch import (
    ClioCoderError,
    ClioCoderInstall,
    ClioCoderTarget,
    InstanceHome,
)
from clio_agent.runtime.clio_coder.results import (
    OUTCOME_FAILED,
    OUTCOME_SUCCEEDED,
    DelegationResult,
    command_text,
)
from clio_agent.runtime.clio_coder.runs import (
    REASON_RUN,
    RUN_OPEN,
    RunRecord,
    RunTurn,
    load_run,
    new_run,
    new_run_id,
    run_lock,
    save_run,
    valid_run_id,
)
from clio_agent.runtime.clio_coder.workspace import (
    ACCEPTANCE_CHECK_ID,
    Workspace,
    adopt_directory,
    collect_changes,
    commit_changes,
    create_worktree,
    declare_acceptance_check,
    remove_worktree,
    restore_worktree,
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
    #: Keep an ACP worktree delegation open for :func:`continue_run` (needs ``keep_worktree``).
    resumable: bool = False

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


@dataclass
class ContinueRequest:
    """A further turn of an open run. ``verify_command``/``verify_timeout`` of ``None`` keep the
    run's acceptance check; a value replaces it from this turn on."""

    run_id: str
    task: str
    state_root: Path
    follow_ups: Sequence[str] = ()
    target: ClioCoderTarget | None = None
    home_template: Path | None = None
    child_env: Mapping[str, str] = field(default_factory=dict)
    options: RunOptions = field(default_factory=RunOptions)
    verify_command: Sequence[str] | None = None
    verify_timeout: float | None = None
    install: ClioCoderInstall | None = None


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
    resumable = request.resumable and surface == SURFACE_ACP and request.isolate
    if resumable and not request.keep_worktree:
        raise ValueError("a resumable run keeps its worktree")
    install = request.install or ClioCoderInstall.resolve()
    state_root = request.state_root.resolve()
    home = InstanceHome(state_root / "home", extra_env=dict(request.child_env))
    home.prepare(request.target, template=request.home_template)
    run_id = request.run_id or new_run_id()
    if not valid_run_id(run_id):
        raise ClioCoderError(REASON_RUN, f"not a Clio Coder run id: {run_id!r}")

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
        await _hand_back(result, workspace, request.task, verify_command, request.verify_timeout)
        if resumable and result.session_id is not None:
            record = new_run(workspace, run_id, verify_command, request.verify_timeout)
            record.session_id = result.session_id
            _record_turn(record, result, request.task)
            save_run(state_root, record)
        # A resumable run that never got a session has no record to continue or discard it by.
        handed_back = not (resumable and result.session_id is None)
    finally:
        # A run that raised has nothing to hand back, so its worktree is never kept.
        if not (request.keep_worktree and handed_back):
            remove_worktree(workspace)
    return result


async def continue_run(
    request: ContinueRequest,
    *,
    sink: EventSink | None = None,
    on_permission: PermissionHandler = reject_permissions,
) -> DelegationResult:
    """Give an open run one more turn: resume its session in its worktree, commit, check.

    A turn that raises (cancelled, or the channel failed to open) leaves no partial edits: the
    worktree goes back to the run's last commit and the run stays open for another turn.
    """
    state_root = request.state_root.resolve()
    with run_lock(state_root, request.run_id):
        record = load_run(state_root, request.run_id)
        if record.status != RUN_OPEN:
            raise ClioCoderError(
                REASON_RUN, f"run {record.run_id} is {record.status}", {"run": record.run_id}
            )
        workspace = record.workspace
        if record.session_id is None or not workspace.path.is_dir():
            raise ClioCoderError(
                REASON_RUN,
                f"run {record.run_id} has lost its worktree; discard it",
                {"run": record.run_id, "worktree": record.worktree},
            )
        install = request.install or ClioCoderInstall.resolve()
        home = InstanceHome(state_root / "home", extra_env=dict(request.child_env))
        home.prepare(request.target, template=request.home_template)
        # Last turn's check may have left artifacts; this turn's diff starts from the branch.
        restore_worktree(workspace)
        rebase = _path_rebaser(Path(record.repo), workspace.path)
        if request.verify_command is not None:
            record.verify_command = [rebase(arg) for arg in request.verify_command] or None
        if request.verify_timeout is not None:
            record.verify_timeout = request.verify_timeout
        verify_command = record.verify_command
        declared = bool(verify_command) and declare_acceptance_check(
            workspace, verify_command or (), record.verify_timeout
        )
        task = compose_task(rebase(request.task), verify_command, declared)
        try:
            result = await run_acp(
                install,
                home,
                workspace.path,
                [task, *(rebase(prompt) for prompt in request.follow_ups)],
                request.options,
                sink,
                on_permission,
                resume_session=record.session_id,
            )
            await _hand_back(result, workspace, request.task, verify_command, record.verify_timeout)
        except BaseException:
            # The turn may have committed before it raised (a cancel during the check).
            with contextlib.suppress(ClioCoderError):
                restore_worktree(workspace, record.tip or record.base_commit)
            raise
        _record_turn(record, result, request.task)
        save_run(state_root, record)
        return result


async def _hand_back(
    result: DelegationResult,
    workspace: Workspace,
    task: str,
    verify_command: Sequence[str] | None,
    verify_timeout: float,
) -> None:
    """Commit what the turn changed on the worktree branch, then run the parent's check."""
    result.workspace = str(workspace.path)
    result.branch = workspace.branch
    result.base_commit = workspace.base_commit
    try:
        result.changed_files, result.diff = collect_changes(workspace)
        result.commit = commit_changes(workspace, _commit_message(task))
    except ClioCoderError as exc:
        # An uncommitted turn has no tip to check or land; never let an older tip stand for it.
        result.error = result.error or exc.to_dict()
        result.outcome, result.outcome_detail = OUTCOME_FAILED, "commit_failed"
        return
    # A failed turn that changed nothing would only re-check the base and read as a pass.
    if verify_command and (result.changed_files or result.outcome == OUTCOME_SUCCEEDED):
        result.verification = await run_verification(
            workspace, verify_command, timeout=verify_timeout
        )


def _record_turn(record: RunRecord, result: DelegationResult, task: str) -> None:
    """Append the turn; the hand-back names the run, the turn and the run's branch tip."""
    result.commit = result.commit or record.tip
    record.session_id = result.session_id or record.session_id
    record.turns.append(
        RunTurn(
            task=task,
            outcome=result.outcome,
            outcome_detail=result.outcome_detail,
            commit=result.commit,
            verified=result.verified if record.verify_command else None,
            usage=dict(result.usage),
        )
    )
    result.run_id, result.turn = record.run_id, len(record.turns)
    total: dict = {}
    for turn in record.turns:
        total = add_usage(total, turn.usage)
    result.run_usage = total
