"""``land_clio_coder``: land a verified Clio Coder run into the checkout, or discard it.

Landing is CLIO's step, run by the server rather than the agent's fenced shell (which sees the
checkout's ``.git`` read-only). It applies the run's accepted commits to the checkout's working
tree and removes the run's worktree and branch; the person reviews and commits the change.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

from clio_agent.gact.agents.tool_instrumentation import declare_structured_content, native_tool
from clio_agent.gact.clio_coder_tool import (
    DIFF_LIMIT,
    _active_session,
    _bounded,
    clio_coder_state_root,
)
from clio_agent.runtime.clio_coder import ClioCoderError, land_run
from clio_agent.runtime.clio_coder.landing import ACTION_APPLY, ACTION_DISCARD
from clio_agent.runtime.clio_coder.runs import valid_run_id

TOOL_NAME = "land_clio_coder"


async def _approve(app: Any, args: dict[str, Any], summary: str) -> None:
    from clio_agent.gact.permission_gate import _invoke_permission_gate  # noqa: PLC0415

    gate = getattr(app.state, "pending_permission_gate", None) or app.state.make_permission_gate()
    decision = await asyncio.to_thread(
        _invoke_permission_gate, gate, TOOL_NAME, args, {"kind": "native", "summary": summary}
    )
    if decision != "allow":
        raise PermissionError(
            f"{args['action'].capitalize()} of the Clio Coder run was not approved"
        )


async def land_clio_coder(run: str, action: str = ACTION_APPLY) -> dict[str, Any]:
    """Finish a Clio Coder run: apply its change to this workspace's checkout, or discard it.

    run is the id run_clio_coder returned. action `apply` lands the run's committed change into
    the checkout's working tree (uncommitted, for the person to review and commit) and refuses
    unless the run's last turn succeeded and its acceptance check passed on that commit, the
    branch has not moved since, and the checkout has no uncommitted edits in the files it
    touches. action `discard` drops the run. Either way the run's worktree and branch are
    removed and the run ends; continue_run no longer accepts it.
    """
    run = str(run or "").strip()
    if not valid_run_id(run):
        raise ValueError(f"run must be a run id run_clio_coder returned: {run!r}")
    if action not in (ACTION_APPLY, ACTION_DISCARD):
        raise ValueError(f"action must be {ACTION_APPLY} or {ACTION_DISCARD}")
    app, _session, root = _active_session()
    verb = "Apply" if action == ACTION_APPLY else "Discard"
    target = f" into the checkout {root}" if action == ACTION_APPLY else ""
    await _approve(app, {"run": run, "action": action}, f"{verb} Clio Coder run {run}{target}")
    try:
        landed = await asyncio.to_thread(land_run, clio_coder_state_root(root), root, run, action)
    except ClioCoderError as exc:
        raise RuntimeError(str(exc)) from exc
    row = landed.to_dict()
    row["run"] = row.pop("run_id")
    row["diff"], row["diff_truncated"] = _bounded(landed.diff, DIFF_LIMIT)
    declare_structured_content(row)
    return row


def land_start_presentation(args: Mapping[str, Any]) -> dict[str, Any]:
    action = "Land" if args.get("action", ACTION_APPLY) == ACTION_APPLY else "Discard"
    return {"action": f"{action} Clio Coder run {args.get('run', '')}", "summary": "", "blocks": []}


def land_presentation(args: Mapping[str, Any], result: Any, structured: Any) -> dict[str, Any]:
    row = structured if isinstance(structured, Mapping) else result
    if not isinstance(row, Mapping):
        return {
            "summary": "Clio Coder land result unavailable",
            "blocks": [],
            "diagnostic": "invalid",
        }
    presentation = land_start_presentation(args)
    blocks = presentation["blocks"]
    if row.get("action") == ACTION_DISCARD:
        presentation["summary"] = "discarded"
        blocks.append(
            {"id": "discarded", "type": "text", "text": f"Removed branch {row.get('branch')}."}
        )
        return presentation
    changed = row.get("changed_files") or []
    presentation["summary"] = (
        f"{len(changed)} file{'s' if len(changed) != 1 else ''} applied to the working tree"
    )
    if row.get("diff"):
        blocks.append({"id": "diff", "type": "diff", "text": str(row["diff"])})
    blocks.append(
        {
            "id": "landed",
            "type": "text",
            "text": (
                f"Applied {str(row.get('commit'))[:12]} from branch {row.get('branch')} to "
                f"{row.get('checkout')} (uncommitted); the run's worktree and branch are removed."
            ),
        }
    )
    return presentation


def build_land_clio_coder_tool() -> Any:
    return native_tool(
        land_clio_coder,
        name=TOOL_NAME,
        desc=land_clio_coder.__doc__,
        args={
            "run": {
                "type": "string",
                "minLength": 1,
                "description": "The run id run_clio_coder returned.",
            },
            "action": {
                "type": "string",
                "enum": [ACTION_APPLY, ACTION_DISCARD],
                "default": ACTION_APPLY,
                "description": "apply lands the verified change; discard drops the run.",
            },
        },
        title="Land Clio Coder run",
        domain="agents",
        presentation=land_presentation,
        presentation_start=land_start_presentation,
    )
