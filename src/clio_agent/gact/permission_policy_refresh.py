"""Apply an explicit sticky tool grant to matching requests already waiting."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from clio_agent.gact.runtime.grant_resolver import EXTERNAL_MCP_CONTEXT_KIND, resolve
from clio_agent.gact.runtime.permission_policies import _permission_path_from_args
from clio_agent.gact.side_sessions import policy_mode


def settle_pending_tool_grants(app: Any, source: Mapping[str, Any]) -> None:
    """Wake covered tool requests while preserving scope, denies and special gates.

    Called under the permission-resolution lock after the user's grant is saved.
    Both the new grant and the complete policy set must allow a waiting call.
    Folder, egress and deferred-hook decisions retain their separate boundaries.
    """
    from clio_agent.gact.permission_gate import _policy_action_for_tool, resolve_permission

    policy = source.get("policy")
    if not isinstance(policy, dict) or source.get("kind", "") not in {
        "",
        EXTERNAL_MCP_CONTEXT_KIND,
    }:
        return
    for pid, row in list(app.state.permissions.items()):
        if row.get("status") != "pending" or row.get("kind", "") not in {
            "",
            EXTERNAL_MCP_CONTEXT_KIND,
        }:
            continue
        sid = str(row.get("session_id") or "")
        session = app.state.sessions.get(sid)
        if session is None:
            continue
        call = row.get("tool_call") or {}
        if not isinstance(call, Mapping):
            continue
        name = str(call.get("tool_name") or "")
        args = call.get("input") or {}
        if not name or not isinstance(args, Mapping):
            continue
        covered = resolve(
            "tool",
            name,
            policies=[policy],
            session_id=sid,
            workspace_id=session.workspace_id,
            path=_permission_path_from_args(args),
        )
        if (
            covered != "allow"
            or _policy_action_for_tool(
                app,
                session_id=sid,
                session=session,
                tool_name=name,
                args=args,
                mode=policy_mode(session),
            )
            != "allow"
        ):
            continue
        row["reason"] = "permission_policy_updated"
        row["policy_refresh_source"] = source["id"]
        resolve_permission(app, pid, "allow", grantor="system")
