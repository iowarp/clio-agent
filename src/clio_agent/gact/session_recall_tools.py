"""Session recall tools: search and read past sessions' retained transcripts.

These are not long-term memory (``memory_*`` tools); they read what happened.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def _active() -> tuple[Any, str, Any]:
    from clio_agent.gact import context  # noqa: PLC0415

    app = context.active_app()
    session_id = context.active_session_id()
    if app is None or not session_id:
        raise ValueError("session recall tool called outside an active session")
    session = app.state.sessions.get(session_id)
    if session is None:
        raise ValueError(f"active session not found: {session_id}")
    return app, session_id, session


def _caller(agent_def: Any) -> dict[str, str]:
    return {"agent_id": str(getattr(agent_def, "id", "") or "main")}


def _session_address(app: Any, session_id: str) -> dict[str, Any]:
    """Link authorized recall results to stable mail and exact finite-run handles."""
    from clio_agent.gact.task_access import TaskAccess

    _, caller_id, _ = _active()
    access = TaskAccess(app.state.task_history, app.state.conversations.caller(caller_id))
    if session_id not in access.session_candidates("read"):
        raise ValueError("Session is unavailable for this operation")
    task = app.state.task_history.latest_assignment(session_id)
    return {
        "session_id": session_id,
        "task_handle": task.handle if task else None,
        "kind": task.kind if task else "Session",
        "status": task.status if task else "unassigned",
        "activity": task.activity if task else "idle",
        "supported_actions": access.view(task)["supported_actions"] if task else [],
        "message_target": session_id if access.session_message_right(session_id) else None,
    }


def _search_result(
    agent_def: Any,
    query: str,
    scope: str,
    limit: int,
    user_intent: str,
) -> dict[str, Any]:
    from clio_agent.gact.routes.session_recall import _recall_tool_audit

    app, session_id, active = _active()
    normalized_scope = scope.strip() or "session"
    normalized_intent = user_intent.strip()
    bounded_limit = max(1, min(int(limit), 50))
    active_workspace = str(getattr(active, "workspace_id", "") or "")
    caller = _caller(agent_def)

    from clio_agent.gact.runtime.session_recall_search import agent_session_search

    response = agent_session_search(
        app, session_id, query=query, scope=normalized_scope, limit=bounded_limit
    )
    policy_decision = response.metadata["policy_decision"]
    policy_scope = response.metadata["policy_scope"]
    audit = _recall_tool_audit(
        app,
        tool_name="session_search",
        session_id=session_id,
        caller=caller,
        policy_decision=policy_decision,
        status="completed",
        scope=policy_scope,
        query=query,
        details={
            "searched_sessions": response.searched_sessions,
            "hit_count": len(response.hits),
            "workspace_id": active_workspace,
            "user_intent": normalized_intent,
        },
    )
    return {
        "tool": "session_search",
        "query": response.query,
        "searched_sessions": response.searched_sessions,
        "hits": [
            {**hit.model_dump(), "agent_target": _session_address(app, hit.session_id)}
            for hit in response.hits
        ],
        "metadata": response.metadata
        | {
            "policy_decision": policy_decision,
            "policy_scope": policy_scope,
            "audit_id": audit["id"],
            "caller": caller,
        },
    }


def _summary_result(
    agent_def: Any,
    target_session_id: str,
    scope: str,
    user_intent: str,
) -> dict[str, Any]:
    from clio_agent.gact.routes.session_recall import (  # noqa: PLC0415
        _raise_recall_policy_denied,
        _recall_session_summary,
        _recall_tool_audit,
        _recall_tool_policy,
    )

    app, session_id, _active_session = _active()
    target_id = target_session_id.strip() or session_id
    caller = _caller(agent_def)
    body = {"scope": scope, "user_intent": user_intent}
    policy = _recall_tool_policy(
        app,
        session_id=session_id,
        target_session_id=target_id,
        body=body,
    )
    if not policy.get("allowed"):
        _raise_recall_policy_denied(
            app,
            tool_name="session_read_summary",
            session_id=session_id,
            target_session_id=target_id,
            caller=caller,
            policy=policy,
        )
    summary = _recall_session_summary(app, target_id, caller_id=session_id)
    audit = _recall_tool_audit(
        app,
        tool_name="session_read_summary",
        session_id=session_id,
        target_session_id=target_id,
        caller=caller,
        policy_decision=str(policy.get("decision") or ""),
        status="completed",
        scope=str(policy.get("scope") or ""),
        details={
            "workspace_id": policy.get("workspace_id", ""),
            "target_workspace_id": summary["workspace_id"],
            "message_count": summary["message_count"],
        },
    )
    return {
        "tool": "session_read_summary",
        "summary": summary,
        "agent_target": _session_address(app, target_id),
        "metadata": {
            "policy_decision": policy.get("decision", ""),
            "policy_scope": policy.get("scope", ""),
            "audit_id": audit["id"],
            "caller": caller,
        },
    }


def _frame_result(
    agent_def: Any,
    target_session_id: str,
    frame_id: str,
    scope: str,
    user_intent: str,
) -> dict[str, Any]:
    from clio_agent.gact.routes.session_recall import (  # noqa: PLC0415
        _bounded_context_frame,
        _raise_recall_policy_denied,
        _recall_tool_audit,
        _recall_tool_error,
        _recall_tool_policy,
    )

    app, session_id, _active_session = _active()
    target_id = target_session_id.strip() or session_id
    normalized_frame_id = frame_id.strip()
    caller = _caller(agent_def)
    if not normalized_frame_id:
        raise _recall_tool_error(
            status_code=422,
            error="invalid_request",
            message="frame_id is required",
            details={"tool_name": "session_read_context_frame"},
            recoverable=True,
        )
    policy = _recall_tool_policy(
        app,
        session_id=session_id,
        target_session_id=target_id,
        body={"scope": scope, "user_intent": user_intent},
    )
    if not policy.get("allowed"):
        _raise_recall_policy_denied(
            app,
            tool_name="session_read_context_frame",
            session_id=session_id,
            target_session_id=target_id,
            caller=caller,
            policy=policy,
        )
    frame: Mapping[str, Any] | None = next(
        (
            row
            for row in app.state.context_frames.get(target_id, [])
            if isinstance(row, Mapping) and row.get("id") == normalized_frame_id
        ),
        None,
    )
    if frame is None:
        raise _recall_tool_error(
            status_code=404,
            error="not_found",
            message=f"context frame not found: {normalized_frame_id}",
            details={"session_id": target_id, "frame_id": normalized_frame_id},
            recoverable=False,
        )
    bounded_frame = _bounded_context_frame(frame)
    audit = _recall_tool_audit(
        app,
        tool_name="session_read_context_frame",
        session_id=session_id,
        target_session_id=target_id,
        caller=caller,
        policy_decision=str(policy.get("decision") or ""),
        status="completed",
        scope=str(policy.get("scope") or ""),
        details={"frame_id": normalized_frame_id},
    )
    return {
        "tool": "session_read_context_frame",
        "frame": bounded_frame,
        "metadata": {
            "policy_decision": policy.get("decision", ""),
            "policy_scope": policy.get("scope", ""),
            "audit_id": audit["id"],
            "caller": caller,
        },
    }


def build_session_search_tool(agent_def: Any) -> Any:
    """Build the bounded retained-session search tool."""

    from clio_agent.gact.agents.tool_instrumentation import native_tool  # noqa: PLC0415

    def session_search(
        query: str,
        scope: str = "session",
        limit: int = 10,
        user_intent: str = "",
    ) -> dict[str, Any]:
        return _search_result(agent_def, query, scope, limit, user_intent)

    return native_tool(
        session_search,
        name="session_search",
        presentation="memory",
        domain="memory",
        title="Search past sessions",
        representation="row",
        desc=(
            "Search retained transcript excerpts in your session, delegated tree and explicitly granted peers. "
            "Scope authorized includes permitted sessions across workspaces. Intent text grants no rights."
        ),
        args={
            "query": {"type": "string", "description": "Text to find."},
            "scope": {
                "type": "string",
                "description": "session, current_workspace, global, or authorized.",
            },
            "limit": {"type": "integer", "description": "Maximum matches from 1 through 50."},
            "user_intent": {
                "type": "string",
                "description": "The user's reason for any cross-session search.",
            },
        },
        read_only=True,
    )


def build_session_summary_tool(agent_def: Any) -> Any:
    """Build the bounded session-summary reader."""

    from clio_agent.gact.agents.tool_instrumentation import native_tool  # noqa: PLC0415

    def session_read_summary(
        target_session_id: str,
        scope: str = "session",
        user_intent: str = "",
    ) -> dict[str, Any]:
        return _summary_result(agent_def, target_session_id, scope, user_intent)

    return native_tool(
        session_read_summary,
        name="session_read_summary",
        presentation="memory",
        domain="memory",
        title="Read session activity",
        representation="row",
        desc="Read a compact summary of a past session's recent activity. For a running task prefer observe_tasks(view='summary') on its handle.",
        args={
            "target_session_id": {"type": "string", "description": "Session to summarize."},
            "scope": {"type": "string", "description": "Requested memory scope."},
            "user_intent": {
                "type": "string",
                "description": "The user's reason for a cross-session read.",
            },
        },
        read_only=True,
    )


def build_session_context_frame_tool(agent_def: Any) -> Any:
    """Build the bounded retained-context reader."""

    from clio_agent.gact.agents.tool_instrumentation import native_tool  # noqa: PLC0415

    def session_read_context_frame(
        target_session_id: str,
        frame_id: str,
        scope: str = "session",
        user_intent: str = "",
    ) -> dict[str, Any]:
        return _frame_result(agent_def, target_session_id, frame_id, scope, user_intent)

    return native_tool(
        session_read_context_frame,
        name="session_read_context_frame",
        presentation="memory",
        domain="memory",
        title="Read context frame",
        representation="row",
        desc=(
            "Read one bounded retained context frame with source and policy provenance. "
            "The full transcript is never returned."
        ),
        args={
            "target_session_id": {"type": "string", "description": "Owning session."},
            "frame_id": {"type": "string", "description": "Retained context frame identifier."},
            "scope": {"type": "string", "description": "Requested memory scope."},
            "user_intent": {
                "type": "string",
                "description": "The user's reason for a cross-session read.",
            },
        },
        read_only=True,
    )


def build_session_recall_tools(agent_def: Any) -> list[Any]:
    """Build all retained-memory tools in stable prompt order."""

    return [
        build_session_search_tool(agent_def),
        build_session_summary_tool(agent_def),
        build_session_context_frame_tool(agent_def),
    ]


__all__ = [
    "build_session_context_frame_tool",
    "build_session_search_tool",
    "build_session_summary_tool",
    "build_session_recall_tools",
]
