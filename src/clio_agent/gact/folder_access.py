"""Human-approved folder access on the existing permission ledger.

Built-in file tools run in process and shell children are confined per invocation.
An approved root therefore takes effect before dispatch, without closing a fleet
leased by the current turn. Persistent external servers retain drain-aware restart.
"""

from __future__ import annotations

import platform
import threading
import uuid
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from clio_agent.gact import permission_timeout
from clio_agent.gact.permission_delivery import publish_permission_event
from clio_agent.gact.runtime.retention import enforce_dict_bound
from clio_agent.tools.file_policy import FileAccessPolicy, FilePolicyError

if TYPE_CHECKING:
    from fastapi import FastAPI

FOLDER_ACCESS_KIND = "folder_access"
FOLDER_RESOLUTION_LOCK = threading.RLock()


def bind_folder_gate(app: FastAPI, gate: Callable[..., str]) -> Callable[..., str]:
    """Ground direct permission calls in their session's actual workspace."""

    def grounded(
        name: str, args: Mapping[str, Any], context: Mapping[str, Any] | None = None
    ) -> str:
        from clio_agent.gact.permission_gate import _resolve_tool_session
        from clio_agent.tools.execution import (
            get_active_tool_workspace_root,
            tool_workspace_context,
        )

        if get_active_tool_workspace_root():
            return gate(name, args, context)
        _, session = _resolve_tool_session(app)
        workspace = app.state.workspaces.get(getattr(session, "workspace_id", ""))
        with tool_workspace_context(getattr(workspace, "root_path", None)):
            return gate(name, args, context)

    return grounded


def requested_folders(name: str, args: Mapping[str, Any]) -> list[str]:
    """Return denied folders for declared built-in path arguments, without reading files."""
    paths: list[tuple[str, bool]] = []
    if name in {"fs_read_file", "fs_propose_edit", "fs_apply_edit_write"}:
        value = args.get("filepath")
        if isinstance(value, str) and value:
            paths.append((value, False))
    elif name == "shell_bash":
        cwd = args.get("cwd")
        if isinstance(cwd, str) and cwd:
            paths.append((cwd, True))
        declared = args.get("access_paths") or []
        if not isinstance(declared, list) or not all(isinstance(p, str) and p for p in declared):
            raise ValueError("access_paths must contain non-empty file or folder paths")
        paths.extend((p, Path(p).expanduser().is_dir()) for p in declared)
    else:
        return []
    policy = FileAccessPolicy.from_env()
    folders: list[str] = []
    for raw, directory in paths:
        path = Path(raw).expanduser()
        # Grounding normally happens at dispatch; keep direct gate callers consistent.
        if not path.is_absolute():
            from clio_agent.tools.execution import get_active_tool_workspace_root

            path = Path(get_active_tool_workspace_root() or Path.cwd()) / path
        # A root grant cannot override connected-source credential/read-only boundaries.
        policy._check_connected_storage(path, field="path", write=name != "fs_read_file")
        from clio_agent.tools.file_policy import _has_symlink

        if not policy.allow_symlinks and _has_symlink(path):
            raise PermissionError("Use a real path; folder approval cannot override symlink policy")
        path = path.resolve(strict=False)
        folder = path if directory else path.parent
        try:
            policy._ensure_allowed(folder, field="path")
        except FilePolicyError as exc:
            if exc.code != "outside_allowed_roots":
                raise
            if str(folder) not in folders:
                folders.append(str(folder))
    return folders


def request_folder_access(
    app: FastAPI, sid: str, session: Any, name: str, args: Mapping[str, Any], folders: list[str]
) -> str:
    """Park the original operation until a human grants its exact folders or declines."""
    from clio_agent.gact.permission_gate import DenyDecision
    from clio_agent.gact.runtime.grant_resolver import resolve

    wid = str(getattr(session, "workspace_id", "") or "")
    if not sid or not wid or app.state.workspaces.get(wid) is None:
        return DenyDecision("Folder access needs an active workspace and user approval")
    for folder in folders:
        if (
            resolve(
                "fs_root",
                folder,
                policies=getattr(app.state, "permission_policies", []),
                session_id=sid,
                workspace_id=wid,
                path=folder,
            )
            == "deny"
        ):
            return DenyDecision(f"Folder access denied by policy: {folder}")
    pid = f"perm_{uuid.uuid4().hex[:12]}"
    event = threading.Event()
    row: dict[str, Any] = {
        "id": pid,
        "kind": FOLDER_ACCESS_KIND,
        "session_id": sid,
        "workspace_id": wid,
        "tool_call": {"tool_name": name, "input": dict(args)},
        "summary": "Allow access to another folder?"
        if len(folders) == 1
        else "Allow access to these folders?",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "status": "pending",
        "actions": ["deny", "allow_workspace"],
        "access": {
            "folders": folders,
            "computer": platform.node(),
            "workspace_id": wid,
            "operation": name,
            "command": str(args.get("command") or ""),
            "paths": [str(args["filepath"])]
            if "filepath" in args
            else list(args.get("access_paths") or []),
            "cwd": str(args.get("cwd") or getattr(session, "workspace_root", "") or ""),
            "reason": str(
                args.get("access_reason")
                or "This operation needs a path outside the workspace's permitted folders."
            ),
            "access": "read_write",
            "scope": "workspace",
        },
    }
    app.state.permissions[pid] = row
    app.state.permission_events[pid] = event
    enforce_dict_bound(app, app.state.permissions, "permissions", session_id=sid)
    publish_permission_event(app, "permission.requested", owner_session_id=sid, payload=row)
    if not event.wait(timeout=permission_timeout.PERMISSION_REQUEST_TIMEOUT_S):
        if expired := permission_timeout.expire_permission(app, pid, row, tool_name=name):
            return expired
    if row.get("action") == "allow_workspace":
        # Recheck immediately before dispatch; a concurrent revoke is not an approval.
        if not requested_folders(name, args):
            return "allow"
        return DenyDecision("Folder access was revoked before the operation could start")
    return DenyDecision(
        "The user declined folder access. Do not retry that path without a new user request; "
        "continue with permitted files or explain the alternative."
    )


def apply_folder_permission(app: FastAPI, row: dict[str, Any], action: str, grantor: str) -> None:
    """Apply actual workspace/sandbox grants before publishing approval or waking dispatch."""
    if action not in row["actions"]:
        raise ValueError("Folder access supports Deny or Allow for workspace")
    if action == "deny":
        return
    if grantor != "user":
        raise PermissionError("Only the user can add a permitted folder")
    from fastapi import HTTPException

    from clio_agent.gact.runtime.grant_resolver import resolve
    from clio_agent.gact.runtime.grants import apply_root_grant

    if app.state.workspaces.get(row["workspace_id"]) is None:
        raise HTTPException(status_code=409, detail="This workspace no longer exists")
    for folder in row["access"]["folders"]:
        if Path(folder).resolve() != Path(folder):
            raise HTTPException(
                status_code=409, detail="The folder target changed. Request access again."
            )
        if (
            resolve(
                "fs_root",
                folder,
                policies=getattr(app.state, "permission_policies", []),
                session_id=row["session_id"],
                workspace_id=row["workspace_id"],
                path=folder,
            )
            == "deny"
        ):
            raise HTTPException(
                status_code=403, detail="Current policy denies access to this folder"
            )
    results = []
    for folder in row["access"]["folders"]:
        results.append(
            apply_root_grant(
                app,
                row["workspace_id"],
                folder,
                grantor=grantor,
                created_from_permission_id=row["id"],
            )
        )
    row["access_results"] = results
