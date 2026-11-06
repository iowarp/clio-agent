"""A call the sandbox blocked asks the person, with the folder-access card (#1905).

When a confined command or script fails with a confirmed ``write_denied`` diagnosis that
names folders to ask for, the person gets the same card a declared ``access_paths``
request raises: which folders, on which computer, for which command, the working
folder, read and write access for the workspace. The card is a pending interaction, so
the session needs the person and the usual notifications fire. Nothing waits on it:
the blocked call has already ended, and allowing grants the folders for the workspace
(the agent retries when asked; it is told how in the call's result).

A folder the person allows another way (the notice's Allow, Settings) settles the card,
so no stale question stays open.
"""

from __future__ import annotations

import logging
import platform
import uuid
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from clio_agent.gact.folder_access import FOLDER_ACCESS_KIND
from clio_agent.gact.permission_delivery import publish_permission_event
from clio_agent.gact.runtime.retention import enforce_dict_bound
from clio_agent.runtime.sandbox_diagnosis import (
    CONFIRMED,
    KIND_WRITE_DENIED,
    BoundaryDiagnosis,
    result_boundary,
)
from clio_agent.tools.task_envelope import settled_tool_result

if TYPE_CHECKING:
    from fastapi import FastAPI

logger = logging.getLogger(__name__)

#: ``origin`` of a card raised by a sandbox block rather than by a call that declared paths.
ORIGIN_SANDBOX_BOUNDARY = "sandbox_boundary"


def _boundary(result: Any) -> BoundaryDiagnosis | None:
    """The diagnosis on a call's result, looking through a settled native task envelope."""
    return result_boundary(result) or result_boundary(settled_tool_result(result))


def _covers(root: str, folder: str) -> bool:
    """Whether a granted ``root`` contains ``folder``."""
    granted, wanted = Path(root), Path(folder)
    return wanted == granted or granted in wanted.parents


def offer_blocked_folders(
    app: FastAPI,
    sid: str,
    *,
    tool_name: str,
    args: Mapping[str, Any],
    call_id: str,
    result: Any,
    workspace_id: str,
) -> dict[str, Any] | None:
    """Raise the folder-access card for a confirmed blocked write; the row, or ``None``.

    Nothing is raised for a possible (unconfirmed) block, for elevation, for a block
    with no folder to ask for (narrowed territory: plan mode, a read-only task), for a
    session without a workspace, for a folder policy denies, or when an open card of
    this session already asks for the same folders.
    """
    boundary = _boundary(result)
    if (
        boundary is None
        or boundary.kind != KIND_WRITE_DENIED
        or boundary.confidence != CONFIRMED
        or not boundary.request_roots
    ):
        return None
    session = app.state.sessions.get(sid)
    wid = workspace_id or str(getattr(session, "workspace_id", "") or "")
    workspace = app.state.workspaces.get(wid) if wid else None
    if session is None or workspace is None:
        return None
    folders = list(dict.fromkeys(boundary.request_roots))
    for open_row in list(app.state.permissions.values()):
        if (
            open_row.get("kind") == FOLDER_ACCESS_KIND
            and open_row.get("status") == "pending"
            and open_row.get("session_id") == sid
            and set(folders) <= set((open_row.get("access") or {}).get("folders") or [])
        ):
            return None
    from clio_agent.gact.runtime.grant_resolver import resolve  # noqa: PLC0415

    policies = getattr(app.state, "permission_policies", [])
    if any(
        resolve("fs_root", f, policies=policies, session_id=sid, workspace_id=wid, path=f) == "deny"
        for f in folders
    ):
        return None
    pid = f"perm_{uuid.uuid4().hex[:12]}"
    named = ", ".join(Path(folder).name or folder for folder in folders)
    row: dict[str, Any] = {
        "id": pid,
        "kind": FOLDER_ACCESS_KIND,
        "origin": ORIGIN_SANDBOX_BOUNDARY,
        "call_id": call_id,
        "session_id": sid,
        "workspace_id": wid,
        "tool_call": {"tool_name": tool_name, "input": dict(args)},
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
            "operation": tool_name,
            "command": str(args.get("command") or ""),
            "paths": list(boundary.paths),
            "cwd": str(args.get("cwd") or getattr(workspace, "root_path", "") or ""),
            "reason": (
                f"The sandbox blocked this command from writing to {named}. Allowing lets "
                "commands and scripts in this workspace write there; ask the agent to try again."
            ),
            "access": "read_write",
            "scope": "workspace",
        },
    }
    app.state.permissions[pid] = row
    enforce_dict_bound(app, app.state.permissions, "permissions", session_id=sid)
    publish_permission_event(app, "permission.requested", owner_session_id=sid, payload=row)
    logger.info(
        "sandbox block offered folder access permission=%s session=%s call=%s folders=%d",
        pid,
        sid,
        call_id,
        len(folders),
    )
    return row


def settle_offers_for_grant(app: FastAPI, workspace_id: str, granted_root: str) -> list[str]:
    """Settle this workspace's open sandbox cards whose folders ``granted_root`` now covers.

    The person allowed the folder another way, so the card's question is answered: it is
    marked resolved as allowed (by the user, ``settled_by: workspace_grant``) and its
    resolution is published. Returns the settled permission ids.
    """
    settled: list[str] = []
    for row in list(app.state.permissions.values()):
        if (
            row.get("origin") != ORIGIN_SANDBOX_BOUNDARY
            or row.get("status") != "pending"
            or row.get("workspace_id") != workspace_id
        ):
            continue
        folders = (row.get("access") or {}).get("folders") or []
        if not folders or not all(_covers(granted_root, f) for f in folders):
            continue
        row.update(
            status="resolved",
            action="allow_workspace",
            grantor="user",
            settled_by="workspace_grant",
            resolved_at=datetime.now(timezone.utc).isoformat(),
        )
        publish_permission_event(
            app,
            "permission.resolved",
            owner_session_id=str(row.get("session_id") or ""),
            payload={
                "permission_id": row["id"],
                "action": "allow_workspace",
                "session_id": str(row.get("session_id") or ""),
                "grantor": "user",
            },
        )
        settled.append(str(row["id"]))
    return settled


__all__ = ["ORIGIN_SANDBOX_BOUNDARY", "offer_blocked_folders", "settle_offers_for_grant"]
