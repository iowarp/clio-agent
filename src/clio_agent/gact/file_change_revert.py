"""Undo one recorded agent write (``POST /v1/sessions/{sid}/diffs/revert``).

Undo puts the file back as it was before the write: it restores the recorded previous
text, or deletes a file the write created. It runs only when the file on disk is still
exactly what the write produced (its sha256 equals the recorded after-hash), so a later
change, by the agent or by the person, is never overwritten. Every refusal is a typed
:class:`FileChangeRefusal` the route turns into an error envelope:

* ``file_change_not_found`` (404) -- no recorded change with that id in the session;
* ``already_reverted`` (409) -- the change was undone before;
* ``undo_unavailable`` (409) -- no previous text was kept (the row names why);
* ``file_changed_since_write`` (409) -- the file is no longer what the write left;
* ``path_outside_workspace`` / ``file_policy`` / ``policy_denied`` (403) -- the same
  workspace, file-policy and permission boundary as any other write.

The undo goes through the same policy resolver as the live tool gate (as an
``fs_write_file`` call, passing the session's mode), lands an audit row in
``/v1/permissions``, and publishes ``file.diff.reverted``.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from clio_agent.gact.events import Event
from clio_agent.gact.file_changes import file_change_rows, file_change_to_wire
from clio_agent.gact.runtime.retention import ledger_guard
from clio_agent.tools.execution import tool_workspace_context
from clio_agent.tools.file_policy import FilePolicyError, validate_write_path
from clio_agent.tools.fs_write import write_text_with_policy

if TYPE_CHECKING:
    from fastapi import FastAPI

#: The permission-policy tool name an Undo is resolved and audited as.
REVERT_POLICY_TOOL = "fs_write_file"


@dataclass
class FileChangeRefusal(Exception):
    """A typed reason an Undo did not run."""

    code: str
    message: str
    status_code: int = 409
    details: dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"


def _find_change(app: "FastAPI", sid: str, change_id: str) -> dict[str, Any]:
    for row in file_change_rows(app, sid):
        if change_id in {row.get("id"), row.get("part_id")}:
            return row
    raise FileChangeRefusal(
        "file_change_not_found",
        f"no recorded file change {change_id!r} in session {sid}",
        404,
        {"session_id": sid, "id": change_id},
    )


def _bounded_target(app: "FastAPI", session: Any, path: str) -> tuple[Path, str | None]:
    """Resolve the target inside the session's workspace and the write file policy."""

    ws = app.state.workspaces.get(getattr(session, "workspace_id", ""))
    workspace_root = ws.root_path if ws is not None and ws.root_path else None
    target = Path(path).resolve(strict=False)
    if workspace_root is not None:
        try:
            target.relative_to(Path(workspace_root).resolve())
        except ValueError as exc:
            raise FileChangeRefusal(
                "path_outside_workspace",
                f"refused to undo a change to {target} outside workspace root {workspace_root}",
                403,
                {"path": str(target)},
            ) from exc
    try:
        with tool_workspace_context(workspace_root):
            return Path(validate_write_path(str(target), field="path")), workspace_root
    except FilePolicyError as exc:
        raise FileChangeRefusal(
            "file_policy", exc.message, 403, {"path": str(target), "code": exc.code}
        ) from exc


def _check_policy(app: "FastAPI", session: Any, target: Path, args: dict[str, Any]) -> None:
    from clio_agent.gact.permission_gate import (  # noqa: PLC0415
        _policy_action_for_tool,
        _record_resolved_permission,
    )

    action = _policy_action_for_tool(
        app,
        session_id=session.id,
        session=session,
        tool_name=REVERT_POLICY_TOOL,
        args=args,
        mode=str(getattr(session, "mode", "") or ""),
    )
    if action != "deny":
        return
    _record_resolved_permission(
        app,
        session_id=session.id,
        tool_name=REVERT_POLICY_TOOL,
        args=args,
        status="auto_denied",
        action="deny",
        summary=f"undo of a file change blocked by permission policy for {target}",
        reason="policy_deny",
    )
    raise FileChangeRefusal(
        "policy_denied",
        f"a permission policy denies writing {target}",
        403,
        {"path": str(target)},
    )


def _current_sha256(target: Path) -> str:
    try:
        with target.open("rb") as handle:
            return hashlib.file_digest(handle, "sha256").hexdigest()
    except FileNotFoundError:
        return ""


def revert_file_change(app: "FastAPI", session: Any, change_id: str) -> dict[str, Any]:
    """Undo one recorded change; return its wire row. Raises :class:`FileChangeRefusal`."""

    row = _find_change(app, session.id, change_id)
    if row.get("status") == "reverted":
        raise FileChangeRefusal(
            "already_reverted", f"the change to {row['path']} was already undone"
        )
    created = bool(row.get("created"))
    previous = row.get("previous_content")
    if not created and previous is None:
        reason = str(row.get("undo_unavailable_reason") or "")
        raise FileChangeRefusal(
            "undo_unavailable",
            f"the previous content of {row['path']} was not kept ({reason})",
            details={"path": row["path"], "reason": reason},
        )
    target, workspace_root = _bounded_target(app, session, str(row["path"]))
    args = {"filepath": str(target), "revert_of": row["id"], "delete": created}
    _check_policy(app, session, target, args)
    current = _current_sha256(target)
    if current != row["sha256_after"]:
        raise FileChangeRefusal(
            "file_changed_since_write",
            f"{target} changed after the agent wrote it; undo would overwrite that change",
            details={
                "path": str(target),
                "expected_sha256": row["sha256_after"],
                "sha256": current,
            },
        )
    from clio_agent.gact.permission_gate import _record_resolved_permission  # noqa: PLC0415

    _record_resolved_permission(
        app,
        session_id=session.id,
        tool_name=REVERT_POLICY_TOOL,
        args=args,
        status="auto_approved",
        action="allow",
        summary=f"undo: {'delete' if created else 'restore'} {target}",
        reason="user_requested_revert",
    )
    if created:
        target.unlink()
    else:
        with tool_workspace_context(workspace_root):
            written = write_text_with_policy(str(target), str(previous))
        from clio_agent.gact.artifacts.minting import mint_harness_write  # noqa: PLC0415

        mint_harness_write(app, session, str(target), written, tool="diffs.revert")
    with ledger_guard(app):
        row["status"] = "reverted"
        row["reverted_at"] = datetime.now(timezone.utc).isoformat()
        row["previous_content"] = None  # restored; never needed again
    wire = file_change_to_wire(row)
    app.state.bus.publish(
        Event(
            type="file.diff.reverted",
            session_id=session.id,
            payload={"session_id": session.id, "deleted": created, "diff": wire},
        )
    )
    return wire


__all__ = ["REVERT_POLICY_TOOL", "FileChangeRefusal", "revert_file_change"]
