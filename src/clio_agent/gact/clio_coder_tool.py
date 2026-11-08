"""``delegate_to_clio_coder``: hand a coding task to a spawned Clio Coder instance.

Clio Coder works in a disposable git worktree of the conversation's workspace, under a scoped
instance home owned by this workspace's CLIO state. The tool returns the instance's answer, the
change as a diff on its own branch, and the parent-side acceptance check; it never merges.
"""

from __future__ import annotations

import asyncio
import contextlib
import math
import os
import shlex
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from clio_agent.gact import context
from clio_agent.gact.agents.tool_instrumentation import declare_structured_content, native_tool
from clio_agent.paths import workspace_state_dir
from clio_agent.runtime.clio_coder import (
    ClioCoderError,
    ClioCoderInstall,
    ClioCoderTarget,
    DelegationRequest,
    DelegationResult,
    RunOptions,
    delegate_coding_task,
)
from clio_agent.runtime.clio_coder.acp import allow_permissions_once, reject_permissions
from clio_agent.tools.execution import current_tool_runtime

TOOL_NAME = "delegate_to_clio_coder"
#: A seeded Clio Coder home (with ``config/settings.yaml``) every instance starts from.
HOME_TEMPLATE_ENV = "CLIO_AGENT_CLIO_CODER_HOME_TEMPLATE"
#: Or one model route: model id, plus optional endpoint URL and the env var holding its key.
MODEL_ENV = "CLIO_AGENT_CLIO_CODER_MODEL"
URL_ENV = "CLIO_AGENT_CLIO_CODER_URL"
KEY_ENV_ENV = "CLIO_AGENT_CLIO_CODER_API_KEY_ENV"
DIFF_LIMIT = 60_000
TEXT_LIMIT = 20_000
_CANCEL_POLL_SECONDS = 0.5


def clio_coder_available() -> bool:
    try:
        ClioCoderInstall.resolve()
    except ClioCoderError:
        return False
    return True


def _target_from_env() -> ClioCoderTarget | None:
    model = os.environ.get(MODEL_ENV, "").strip()
    if not model:
        return None
    return ClioCoderTarget(
        model=model,
        url=os.environ.get(URL_ENV) or None,
        api_key_env=os.environ.get(KEY_ENV_ENV) or None,
    )


def _workspace_root() -> tuple[Any, str, Path]:
    app, sid = context.active_app(), context.active_session_id()
    session = app.state.sessions.get(sid) if app is not None and sid else None
    workspace = app.state.workspaces.get(session.workspace_id) if session is not None else None
    if workspace is None or not workspace.root_path:
        raise ValueError("Delegating to Clio Coder requires an active workspace conversation")
    return app, sid, Path(workspace.root_path)


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


async def delegate_to_clio_coder(
    task: str,
    follow_ups: list[str] | None = None,
    verify_command: str = "",
    timeout_s: float = 900,
    autonomy: str = "default",
    allow_permission_asks: bool = False,
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
    `yolo` runs without Clio Coder's own asks (its sandbox and safety judge stay on);
    allow_permission_asks approves each ask Clio Coder raises, otherwise every ask is refused.
    timeout_s bounds the whole session (60 to 3600 seconds). The change is committed on the
    returned branch (`commit`); landing it is a separate step, for example
    `git diff <base_commit> <commit> | git apply` in the checkout, a merge, or discarding it.
    """
    if not isinstance(task, str) or not task.strip():
        raise ValueError("Provide a nonempty coding task")
    if isinstance(timeout_s, bool) or not math.isfinite(timeout_s) or not 60 <= timeout_s <= 3600:
        raise ValueError("timeout_s must be between 60 and 3600 seconds")
    if autonomy not in ("default", "yolo"):
        raise ValueError("autonomy must be 'default' or 'yolo'")
    follow_ups = [str(item) for item in follow_ups or [] if str(item).strip()]
    verify = _shell_argv(verify_command) if verify_command.strip() else None
    app, _sid, root = _workspace_root()
    if not (root / ".git").exists():
        raise ValueError(f"Clio Coder delegation needs a git repository; {root} is not one")
    args = {
        "task": task,
        "follow_ups": follow_ups,
        "verify_command": verify_command,
        "timeout_s": timeout_s,
        "autonomy": autonomy,
        "allow_permission_asks": allow_permission_asks,
    }
    await _approve(app, args, root)
    template = os.environ.get(HOME_TEMPLATE_ENV)
    state_root = workspace_state_dir(root) / "clio-coder"
    request = DelegationRequest(
        task=task,
        repo=root,
        state_root=state_root,
        follow_ups=follow_ups,
        target=_target_from_env(),
        home_template=Path(template) if template else None,
        options=RunOptions(autonomy=autonomy, timeout=timeout_s),
        verify_command=verify,
    )
    if request.target is None and request.home_template is None:
        settings = state_root / "home" / "config" / "settings.yaml"
        if not settings.exists():
            raise ValueError(
                f"Clio Coder has no model configured for CLIO: set {MODEL_ENV} (with {URL_ENV} "
                f"and {KEY_ENV_ENV}) or {HOME_TEMPLATE_ENV}, or configure {settings}"
            )
    try:
        result = await _run_cancellable(request, allow_permission_asks)
    except ClioCoderError as exc:
        raise RuntimeError(f"Clio Coder could not run: {exc}") from exc
    row = result_row(result, task, follow_ups)
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


def _command_text(argv: list[str]) -> str:
    if argv[:2] == ["/bin/sh", "-c"] and len(argv) == 3:
        return argv[2]
    if argv[:4] == ["cmd", "/d", "/s", "/c"] and len(argv) == 5:
        return argv[4]
    return shlex.join(argv)


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
    if isinstance(verification, Mapping):
        blocks.append(
            {
                "id": "verification",
                "type": "terminal",
                "command": _command_text(list(verification.get("command") or [])),
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


def build_delegate_to_clio_coder_tool() -> Any:
    return native_tool(
        delegate_to_clio_coder,
        name=TOOL_NAME,
        desc=delegate_to_clio_coder.__doc__,
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
                "enum": ["default", "yolo"],
                "default": "default",
                "description": "Clio Coder autonomy; yolo skips its asks, keeps its sandbox.",
            },
            "allow_permission_asks": {
                "type": "boolean",
                "default": False,
                "description": "Approve each ask Clio Coder raises instead of refusing it.",
            },
        },
        title="Delegate to Clio Coder",
        domain="agents",
        presentation=clio_coder_presentation,
        presentation_start=clio_coder_start_presentation,
    )
