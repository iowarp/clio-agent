"""``run_clio_coder``: hand a coding task to a spawned Clio Coder instance.

Clio Coder works in a disposable git worktree of the conversation's workspace, under a scoped
instance home owned by this workspace's CLIO state. The tool returns the instance's answer, the
change as a diff on its own branch, and the parent-side acceptance check; it never merges.
"""

from __future__ import annotations

import asyncio
import contextlib
import math
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from clio_agent.gact import context
from clio_agent.gact.agents.tool_instrumentation import declare_structured_content, native_tool
from clio_agent.gact.clio_coder_route import resolve_route
from clio_agent.paths import workspace_state_dir
from clio_agent.runtime.clio_coder import (
    ClioCoderError,
    ClioCoderInstall,
    DelegationRequest,
    DelegationResult,
    RunOptions,
    delegate_coding_task,
)
from clio_agent.runtime.clio_coder.acp import allow_permissions_once, reject_permissions
from clio_agent.runtime.clio_coder.results import command_text
from clio_agent.tools.execution import current_tool_runtime

TOOL_NAME = "run_clio_coder"
#: Clio Coder autonomy levels, least to most; a delegation never exceeds the dispatcher's.
AUTONOMY_LEVELS = ("read-only", "default", "yolo")
AUTONOMY_INHERIT = "inherit"
DIFF_LIMIT = 60_000
TEXT_LIMIT = 20_000
_CANCEL_POLL_SECONDS = 0.5


def clio_coder_unavailable_reason() -> str | None:
    """Why the tool is withheld: not installed, or older than CLIO supports; ``None`` if usable."""
    try:
        ClioCoderInstall.resolve().require_supported()
    except ClioCoderError as exc:
        return (
            "clio_coder_too_old"
            if exc.error_type == "clio_coder_too_old"
            else ("clio_coder_not_installed")
        )
    return None


def _active_session() -> tuple[Any, Any, Path]:
    app, sid = context.active_app(), context.active_session_id()
    session = app.state.sessions.get(sid) if app is not None and sid else None
    workspace = app.state.workspaces.get(session.workspace_id) if session is not None else None
    if workspace is None or not workspace.root_path:
        raise ValueError("Delegating to Clio Coder requires an active workspace conversation")
    return app, session, Path(workspace.root_path)


def autonomy_ceiling(session: Any) -> str:
    """The most autonomy this conversation can hand down: its own, never more.

    A plan-mode conversation may delegate only read-only work; only a conversation whose
    approvals are bypassed (CLIO's yolo) may run Clio Coder in yolo.
    """
    if getattr(session, "mode", "edit") == "plan":
        return "read-only"
    return "yolo" if getattr(session, "approval_mode", "ask") == "bypass" else "default"


def resolve_autonomy(requested: str, ceiling: str) -> str:
    """``inherit`` takes the ceiling; an explicit level must not exceed it."""
    if requested == AUTONOMY_INHERIT:
        return ceiling
    if requested not in AUTONOMY_LEVELS:
        raise ValueError(f"autonomy must be one of inherit, {', '.join(AUTONOMY_LEVELS)}")
    if AUTONOMY_LEVELS.index(requested) > AUTONOMY_LEVELS.index(ceiling):
        raise ValueError(
            f"autonomy {requested} exceeds this conversation's autonomy ({ceiling}); a delegated "
            "run may only have the same or less"
        )
    return requested


async def _approve(app: Any, args: dict[str, Any], root: Path) -> None:
    from clio_agent.gact.permission_gate import _invoke_permission_gate  # noqa: PLC0415

    gate = getattr(app.state, "pending_permission_gate", None) or app.state.make_permission_gate()
    summary = f"Delegate a coding task to Clio Coder in a worktree of {root}: {args['task']}"
    decision = await asyncio.to_thread(
        _invoke_permission_gate,
        gate,
        TOOL_NAME,
        args,
        {"kind": "native", "summary": summary[:1000]},
    )
    if decision != "allow":
        raise PermissionError("Delegation to Clio Coder was not approved")


async def _run_cancellable(request: DelegationRequest, allow_asks: bool) -> Any:
    """Run the delegation; the conversation's cancel stops the instance and its worktree."""
    cancelled = current_tool_runtime().cancellation_checker
    work = asyncio.ensure_future(
        delegate_coding_task(
            request, on_permission=allow_permissions_once if allow_asks else reject_permissions
        )
    )
    while not work.done():
        await asyncio.wait({work}, timeout=_CANCEL_POLL_SECONDS)
        if not work.done() and cancelled is not None and cancelled():
            work.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await work
            raise InterruptedError("Clio Coder delegation was cancelled")
    return work.result()


def _bounded(text: str, limit: int) -> tuple[str, bool]:
    return (text, False) if len(text) <= limit else (text[:limit], True)


async def run_clio_coder(
    task: str,
    follow_ups: list[str] | None = None,
    verify_command: str = "",
    timeout_s: float = 900,
    autonomy: str = AUTONOMY_INHERIT,
    allow_permission_asks: bool = False,
    model: str = "",
    provider: str = "",
) -> dict[str, Any]:
    """Hand a self-contained coding task to a fresh Clio Coder instance and get the change back.

    Clio Coder edits a disposable git worktree of this workspace on its own branch, so the
    workspace checkout is untouched; the result is the instance's answer, its outcome, the
    changed files and unified diff, and (when verify_command is set) whether that command
    exits 0 in the worktree. Write the task as a complete brief: goal, files, constraints and
    how to check it, with paths relative to the repository root. follow_ups are further turns in
    the same Clio Coder session. verify_command is a shell command run from the worktree root
    (for example `pytest -q tests/test_x.py`); it is declared to the instance and run again by
    CLIO afterwards; treat verified=false or a failed outcome as unaccepted work. autonomy
    defaults to `inherit` (this conversation's own level) and may only be lowered: `read-only`
    (no writes or commands), `default`, or `yolo` (no Clio Coder asks; its sandbox and safety
    judge stay on), which needs a conversation whose approvals are bypassed.
    allow_permission_asks approves each ask Clio Coder raises (also only when approvals are
    bypassed); otherwise every ask is refused.
    model (with provider, a fleet catalog provider id) picks the model for this call only; leave
    both empty to use Clio Coder's binding, else the fleet default, else this conversation's model.
    timeout_s bounds the whole session (60 to 3600 seconds). The change is committed on the
    returned branch (`commit`); landing it is a separate step, for example
    `git diff <base_commit> <commit> | git apply` in the checkout, a merge, or discarding it.
    """
    if not isinstance(task, str) or not task.strip():
        raise ValueError("Provide a nonempty coding task")
    if isinstance(timeout_s, bool) or not math.isfinite(timeout_s) or not 60 <= timeout_s <= 3600:
        raise ValueError("timeout_s must be between 60 and 3600 seconds")
    follow_ups = [str(item) for item in follow_ups or [] if str(item).strip()]
    verify = _shell_argv(verify_command) if verify_command.strip() else None
    app, session, root = _active_session()
    ceiling = autonomy_ceiling(session)
    autonomy = resolve_autonomy(autonomy, ceiling)
    if allow_permission_asks and ceiling != "yolo":
        raise ValueError(
            "allow_permission_asks approves Clio Coder's asks without the person, so it needs a "
            "conversation whose approvals are bypassed"
        )
    if not (root / ".git").exists():
        raise ValueError(f"Clio Coder delegation needs a git repository; {root} is not one")
    route = resolve_route(app, session, model=str(model or ""), provider=str(provider or ""))
    args = {
        "task": task,
        "follow_ups": follow_ups,
        "verify_command": verify_command,
        "timeout_s": timeout_s,
        "autonomy": autonomy,
        "allow_permission_asks": allow_permission_asks,
        "model": route.describe(),
    }
    await _approve(app, args, root)
    state_root = workspace_state_dir(root) / "clio-coder"
    request = DelegationRequest(
        task=task,
        repo=root,
        state_root=state_root,
        follow_ups=follow_ups,
        target=route.target,
        home_template=route.home_template,
        child_env=route.env,
        options=RunOptions(
            autonomy="default" if autonomy == "read-only" else autonomy,
            read_only=autonomy == "read-only",
            timeout=timeout_s,
            target=route.target.target_id if route.target is not None else None,
            model=route.target.model if route.target is not None else None,
        ),
        verify_command=verify,
    )
    try:
        result = await _run_cancellable(request, allow_permission_asks)
    except ClioCoderError as exc:
        raise RuntimeError(f"Clio Coder could not run: {exc}") from exc
    row = result_row(result, task, follow_ups)
    row["route"] = route.describe()
    declare_structured_content(row)
    return row


def result_row(result: DelegationResult, task: str, follow_ups: list[str]) -> dict[str, Any]:
    """The bounded, model-facing hand-back of one delegation."""
    row = result.to_dict()
    row["diff"], row["diff_truncated"] = _bounded(result.diff, DIFF_LIMIT)
    row["text"], row["text_truncated"] = _bounded(result.text, TEXT_LIMIT)
    row["task"], row["follow_ups"] = task, follow_ups
    row["tools"] = [f"{tool.name}: {tool.status}" for tool in result.tools]
    row.pop("stderr_tail", None)
    # Clio Coder's own failure is a delegation result, not a failure of this tool call.
    row["instance_error"] = row.pop("error", None)
    return row


def _shell_argv(command: str) -> list[str]:
    """Run the check through the platform shell so pipes, ``&&`` and ``cd`` work as written."""
    if os.name == "nt":
        return ["cmd", "/d", "/s", "/c", command]
    return ["/bin/sh", "-c", command]


def _child_check(check: Mapping[str, Any]) -> str:
    if check.get("timedOut"):
        verdict = "timed out"
    elif check.get("passed"):
        verdict = "passed"
    else:
        verdict = f"failed (exit {check.get('exitCode')})"
    after = "; it also ran it after its last edit" if check.get("ranAfterLastEdit") else ""
    return f"Clio Coder's own run of `{check.get('command')}`: {verdict}{after}."


def _row(result: Any, structured: Any) -> Mapping[str, Any] | None:
    row = structured if isinstance(structured, Mapping) else result
    return row if isinstance(row, Mapping) else None


def clio_coder_start_presentation(args: Mapping[str, Any]) -> dict[str, Any]:
    """The assignment, visible while the instance is still working."""
    blocks = [{"id": "assignment", "type": "markdown", "text": str(args.get("task", ""))}]
    for index, follow_up in enumerate(args.get("follow_ups") or []):
        blocks.append({"id": f"follow-up-{index}", "type": "text", "text": f"Then: {follow_up}"})
    return {"action": "Delegate to Clio Coder", "summary": "", "blocks": blocks}


def clio_coder_presentation(
    args: Mapping[str, Any], result: Any, structured: Any
) -> dict[str, Any]:
    """Show what Clio Coder said, what it changed, and whether the parent's check passed."""
    row = _row(result, structured)
    if row is None:
        return {"summary": "Clio Coder result unavailable", "blocks": [], "diagnostic": "invalid"}
    presentation = clio_coder_start_presentation(args)
    blocks = presentation["blocks"]
    outcome = str(row.get("outcome") or "failed")
    changed = row.get("changed_files") or []
    verification = row.get("verification")
    accepted = outcome == "succeeded" and (verification is None or bool(row.get("verified")))
    parts = [f"{len(changed)} file{'s' if len(changed) != 1 else ''} changed"]
    if verification is not None:
        parts.append("check passed" if row.get("verified") else "check failed")
    if outcome != "succeeded":
        detail = row.get("outcome_detail")
        parts.append(f"Clio Coder {outcome}" + (f" ({detail})" if detail else ""))
    presentation["summary"] = ", ".join(parts)
    presentation["status"] = "completed" if accepted else "failed"
    if row.get("text"):
        blocks.append({"id": "answer", "type": "markdown", "text": str(row["text"])})
    instance_error = row.get("instance_error")
    if isinstance(instance_error, Mapping) and instance_error.get("message"):
        blocks.append(
            {
                "id": "instance-error",
                "type": "text",
                "text": f"Clio Coder reported: {instance_error['message']}",
            }
        )
    if row.get("diff"):
        blocks.append({"id": "diff", "type": "diff", "text": str(row["diff"])})
        if row.get("diff_truncated"):
            blocks.append(
                {
                    "id": "diff-truncated",
                    "type": "text",
                    "text": f"Diff truncated; the full change is on branch {row.get('branch')}.",
                }
            )
    else:
        blocks.append({"id": "no-change", "type": "text", "text": "No files were changed."})
    for index, check in enumerate(row.get("child_checks") or []):
        if isinstance(check, Mapping):
            blocks.append(
                {"id": f"child-check-{index}", "type": "text", "text": _child_check(check)}
            )
    if isinstance(verification, Mapping):
        blocks.append(
            {
                "id": "verification",
                "type": "terminal",
                "command": command_text(list(verification.get("command") or [])),
                "detail": f"Working directory: {row.get('workspace')}",
                "text": str(verification.get("output_tail") or ""),
                "exit_code": verification.get("exit_code"),
                "timed_out": verification.get("exit_code") is None,
            }
        )
    if row.get("branch"):
        blocks.append(
            {
                "id": "branch",
                "type": "text",
                "text": (
                    f"Committed {str(row['commit'])[:12]} on branch {row['branch']}"
                    if row.get("commit")
                    else f"Branch {row['branch']} (no commit; nothing changed)"
                )
                + f" in worktree {row.get('workspace')}",
            }
        )
    return presentation


def build_run_clio_coder_tool() -> Any:
    return native_tool(
        run_clio_coder,
        name=TOOL_NAME,
        desc=run_clio_coder.__doc__,
        args={
            "task": {
                "type": "string",
                "minLength": 1,
                "description": "Complete coding brief: goal, files, constraints, how to check.",
            },
            "follow_ups": {
                "type": "array",
                "items": {"type": "string"},
                "default": [],
                "description": "Further turns in the same Clio Coder session, in order.",
            },
            "verify_command": {
                "type": "string",
                "default": "",
                "description": "Acceptance command run in the worktree; exit 0 means accepted.",
            },
            "timeout_s": {
                "type": "number",
                "minimum": 60,
                "maximum": 3600,
                "default": 900,
                "description": "Deadline for the whole Clio Coder session.",
            },
            "autonomy": {
                "type": "string",
                "enum": [AUTONOMY_INHERIT, *AUTONOMY_LEVELS],
                "default": AUTONOMY_INHERIT,
                "description": "Clio Coder autonomy: this conversation's own (inherit) or less.",
            },
            "allow_permission_asks": {
                "type": "boolean",
                "default": False,
                "description": "Approve each ask Clio Coder raises instead of refusing it.",
            },
            "model": {
                "type": "string",
                "default": "",
                "description": "Model for this call only; empty uses the binding, fleet default "
                "or this conversation's model.",
            },
            "provider": {
                "type": "string",
                "default": "",
                "description": "Fleet provider id for model; empty means this conversation's.",
            },
        },
        title="Delegate to Clio Coder",
        domain="agents",
        presentation=clio_coder_presentation,
        presentation_start=clio_coder_start_presentation,
    )
