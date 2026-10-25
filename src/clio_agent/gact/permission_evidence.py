"""Durable, minimized receipts for tool permission decisions, never access grants."""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Mapping
from typing import Any

from clio_agent.gact.context import active_app, active_assignment_handle, active_session_id

logger = logging.getLogger(__name__)


def permission_identity(app: Any, session_id: str) -> dict[str, str]:
    """Capture the admitted assignment now; HTTP resolution has no such context."""
    sessions = getattr(app.state, "sessions", None)
    session = sessions.get(session_id) if sessions is not None else None
    return {
        "assignment_id": active_assignment_handle()
        if active_app() is app and active_session_id() == session_id
        else "",
        "workspace_id": str(getattr(session, "workspace_id", "") or ""),
    }


def emit_permission_resolution(app: Any, row: Mapping[str, Any]) -> None:
    """Record terminal attribution without copying arguments, paths or credentials.

    Failure to record is reported and never changes an authorization decision.
    Missing historical attribution stays missing; current policy is not history.
    """
    from clio_agent.gact.runtime.globals import _emit_semantic_event  # noqa: PLC0415

    tool = row.get("tool_call")
    tool = tool if isinstance(tool, Mapping) else {}
    policy = row.get("policy")
    policy = policy if isinstance(policy, Mapping) else {}
    reason = str(row.get("reason") or "")
    payload = {
        "permission_id": str(row.get("id") or ""),
        "assignment_id": str(row.get("assignment_id") or ""),
        "workspace_id": str(row.get("workspace_id") or ""),
        "tool_name": str(tool.get("tool_name") or ""),
        "kind": str(row.get("kind") or "tool"),
        "action": str(row.get("action") or ""),
        "decision_status": str(row.get("status") or ""),
        "grantor": str(row.get("grantor") or "system"),
        "resolved_at": str(row.get("resolved_at") or ""),
        "reason": reason if re.fullmatch(r"[a-z][a-z0-9_]*", reason) else "",
        "policy_refresh_source": str(row.get("policy_refresh_source") or ""),
        "policy_scope": str(
            policy.get("scope")
            or ("policy_refresh" if row.get("policy_refresh_source") else "invocation")
        ),
        "policy_scope_id": str(policy.get("scope_id") or ""),
        "resource_restricted": bool(policy.get("path_pattern") or policy.get("host_pattern")),
    }
    try:
        _emit_semantic_event(
            app,
            str(row.get("session_id") or ""),
            "permission.resolved",
            status="completed",
            summary=f"Permission {payload['permission_id']} resolved: {payload['action']}.",
            actor={"role": payload["grantor"]},
            subject={"permission_id": payload["permission_id"], "action": payload["action"]},
            payload=payload,
        )
    except Exception as exc:  # noqa: BLE001 - recording cannot alter the decision
        logger.warning(
            "permission receipt unavailable permission_id=%s error=%r",
            payload["permission_id"],
            exc,
        )


def permission_receipts(app: Any, session_id: str, root_handle: str) -> dict[str, Any]:
    """Read this assignment's permission history from ARC, not the bounded ledger.

    Only confirmed descendants and their original human are admitted. Permission
    events from unrelated assignments in a reused session remain excluded. No
    raw request input or policy patterns leave this boundary. Receipts describe
    past decisions; they cannot authorize a call or prove present permission.
    """
    history = app.state.task_history
    root = history.task_snapshot(root_handle)
    if root is None or root.owner.session_id != session_id:
        raise ValueError("Permission evidence requires the flow's own confirmed assignment")
    handles = history.descendant_handles(root_handle)
    owners = {}
    for handle in handles:
        task = history.task_snapshot(handle)
        if task is not None and task.owner.human_id == root.owner.human_id:
            owners[handle] = task.owner.session_id
    observer = getattr(history.arc, "_live", None)
    iterator = getattr(observer, "iter_session_event_segments", None)
    session = app.state.sessions.get(session_id)
    scope = {
        "assignment_id": root_handle,
        "session_id": session_id,
        "workspace_id": str(getattr(session, "workspace_id", "") or ""),
    }
    if not callable(iterator):
        return {
            "status": "unavailable",
            "reason": "durable_permission_events_unavailable",
            "receipts": [],
            "scope": scope,
        }
    receipts = []
    fields = (
        "permission_id",
        "assignment_id",
        "workspace_id",
        "tool_name",
        "kind",
        "action",
        "decision_status",
        "grantor",
        "resolved_at",
        "reason",
        "policy_refresh_source",
        "policy_scope",
        "policy_scope_id",
        "resource_restricted",
    )
    for sid in sorted(set(owners.values())):
        for segment in iterator(sid):
            event = segment.content
            if event.get("event_type") != "permission.resolved":
                continue
            payload = event.get("payload") or {}
            assignment = payload.get("assignment_id")
            if assignment not in owners or owners[assignment] != sid:
                continue
            if not payload.get("permission_id") or not payload.get("resolved_at"):
                continue
            receipt = {key: payload.get(key) for key in fields}
            receipt["session_id"] = sid
            receipt["receipt_id"] = (
                "permission_"
                + hashlib.sha256(
                    json.dumps(receipt, sort_keys=True, ensure_ascii=False).encode()
                ).hexdigest()
            )
            receipts.append(receipt)
    receipts.sort(key=lambda row: (row["resolved_at"], row["receipt_id"]))
    return {
        "status": "available",
        "coverage": "decisions_with_confirmed_assignment_identity",
        "absence_proves_no_decision": False,
        "receipts": receipts,
        "scope": scope,
    }
