"""Policy-gated retained-memory tools for dynamic agents."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from clio_agent.gact.runtime.memory_search import _memory_search_response
from clio_agent.gact.workspace_scope import GLOBAL_WORKSPACE_ID


def _active() -> tuple[Any, str, Any]:
    from clio_agent.gact import context  # noqa: PLC0415

    app = context.active_app()
    session_id = context.active_session_id()
    if app is None or not session_id:
        raise ValueError("memory tool called outside an active session")
    session = app.state.sessions.get(session_id)
    if session is None:
        raise ValueError(f"active session not found: {session_id}")
    return app, session_id, session


def _caller(agent_def: Any) -> dict[str, str]:
    return {"agent_id": str(getattr(agent_def, "id", "") or "main")}


def _search_result(
    agent_def: Any,
    query: str,
    scope: str,
    limit: int,
    user_intent: str,
) -> dict[str, Any]:
    from clio_agent.gact.routes.memory import _memory_tool_audit, _raise_memory_policy_denied

    app, session_id, active = _active()
    normalized_scope = scope.strip() or "session"
    normalized_intent = user_intent.strip()
    bounded_limit = max(1, min(int(limit), 50))
    active_workspace = str(getattr(active, "workspace_id", "") or "")
    caller = _caller(agent_def)

    if normalized_scope in {"global", "user", "user_global"}:
        if not normalized_intent:
            policy = {
                "decision": "deny_global_requires_intent",
                "scope": "global",
                "workspace_id": active_workspace,
                "target_workspace_id": GLOBAL_WORKSPACE_ID,
                "user_intent": normalized_intent,
            }
            _raise_memory_policy_denied(
                app,
                tool_name="memory_search_sessions",
                session_id=session_id,
                target_session_id="",
                caller=caller,
                policy=policy,
                query=query,
            )
        response = _memory_search_response(
            app,
            query=query,
            workspace_id=GLOBAL_WORKSPACE_ID,
            include_cross_session=True,
            limit=bounded_limit,
        )
        policy_decision = "allow_global_user_intent"
        policy_scope = "global"
    elif normalized_scope in {"current_workspace", "workspace", "cross_session"}:
        if not normalized_intent:
            policy = {
                "decision": "deny_cross_session_requires_intent",
                "scope": "current_workspace",
                "workspace_id": active_workspace,
                "user_intent": normalized_intent,
            }
            _raise_memory_policy_denied(
                app,
                tool_name="memory_search_sessions",
                session_id=session_id,
                target_session_id="",
                caller=caller,
                policy=policy,
                query=query,
            )
        response = _memory_search_response(
            app,
            query=query,
            session_id=session_id,
            workspace_id=active_workspace,
            include_cross_session=True,
            limit=bounded_limit,
        )
        response = response.model_copy(
            update={
                "searched_sessions": [
                    candidate
                    for candidate in response.searched_sessions
                    if candidate != session_id
                ],
                "hits": [hit for hit in response.hits if hit.session_id != session_id],
            }
        )
        policy_decision = "allow_same_workspace_user_intent"
        policy_scope = "current_workspace"
    else:
        response = _memory_search_response(
            app,
            query=query,
            session_id=session_id,
            include_cross_session=False,
            limit=bounded_limit,
        )
        policy_decision = "allow_same_session"
        policy_scope = "session"

    audit = _memory_tool_audit(
        app,
        tool_name="memory_search_sessions",
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
        "tool": "memory_search_sessions",
        "query": response.query,
        "searched_sessions": response.searched_sessions,
        "hits": [hit.model_dump() for hit in response.hits],
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
    from clio_agent.gact.routes.memory import (  # noqa: PLC0415
        _memory_session_summary,
        _memory_tool_audit,
        _memory_tool_policy,
        _raise_memory_policy_denied,
    )

    app, session_id, _active_session = _active()
    target_id = target_session_id.strip() or session_id
    caller = _caller(agent_def)
    body = {"scope": scope, "user_intent": user_intent}
    policy = _memory_tool_policy(
        app,
        session_id=session_id,
        target_session_id=target_id,
        body=body,
    )
    if not policy.get("allowed"):
        _raise_memory_policy_denied(
            app,
            tool_name="memory_read_session_summary",
            session_id=session_id,
            target_session_id=target_id,
            caller=caller,
            policy=policy,
        )
    summary = _memory_session_summary(app, target_id)
    audit = _memory_tool_audit(
        app,
        tool_name="memory_read_session_summary",
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
        "tool": "memory_read_session_summary",
        "summary": summary,
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
    from clio_agent.gact.routes.memory import (  # noqa: PLC0415
        _bounded_context_frame,
        _memory_tool_audit,
        _memory_tool_error,
        _memory_tool_policy,
        _raise_memory_policy_denied,
    )

    app, session_id, _active_session = _active()
    target_id = target_session_id.strip() or session_id
    normalized_frame_id = frame_id.strip()
    caller = _caller(agent_def)
    if not normalized_frame_id:
        raise _memory_tool_error(
            status_code=422,
            error="invalid_request",
            message="frame_id is required",
            details={"tool_name": "memory_read_context_frame"},
            recoverable=True,
        )
    policy = _memory_tool_policy(
        app,
        session_id=session_id,
        target_session_id=target_id,
        body={"scope": scope, "user_intent": user_intent},
    )
    if not policy.get("allowed"):
        _raise_memory_policy_denied(
            app,
            tool_name="memory_read_context_frame",
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
        raise _memory_tool_error(
            status_code=404,
            error="not_found",
            message=f"context frame not found: {normalized_frame_id}",
            details={"session_id": target_id, "frame_id": normalized_frame_id},
            recoverable=False,
        )
    bounded_frame = _bounded_context_frame(frame)
    audit = _memory_tool_audit(
        app,
        tool_name="memory_read_context_frame",
        session_id=session_id,
        target_session_id=target_id,
        caller=caller,
        policy_decision=str(policy.get("decision") or ""),
        status="completed",
        scope=str(policy.get("scope") or ""),
        details={"frame_id": normalized_frame_id},
    )
    return {
        "tool": "memory_read_context_frame",
        "frame": bounded_frame,
        "metadata": {
            "policy_decision": policy.get("decision", ""),
            "policy_scope": policy.get("scope", ""),
            "audit_id": audit["id"],
            "caller": caller,
        },
    }


def build_memory_search_tool(agent_def: Any) -> Any:
    """Build the bounded retained-session search tool."""

    from clio_agent.gact.agents.tool_instrumentation import native_tool  # noqa: PLC0415

    def memory_search_sessions(
        query: str,
        scope: str = "session",
        limit: int = 10,
        user_intent: str = "",
    ) -> dict[str, Any]:
        return _search_result(agent_def, query, scope, limit, user_intent)

    return native_tool(
        memory_search_sessions,
        name="memory_search_sessions",
        presentation="memory",
        title="Search memory",
        representation="row",
        desc=(
            "Search retained session memory. Same-workspace or global search requires explicit "
            "user intent and returns bounded provenance-bearing excerpts."
        ),
        args={
            "query": {"type": "string", "description": "Text to find."},
            "scope": {
                "type": "string",
                "description": "session, current_workspace, or global.",
            },
            "limit": {"type": "integer", "description": "Maximum matches from 1 through 50."},
            "user_intent": {
                "type": "string",
                "description": "The user's reason for any cross-session search.",
            },
        },
    )


def build_memory_summary_tool(agent_def: Any) -> Any:
    """Build the bounded session-summary reader."""

    from clio_agent.gact.agents.tool_instrumentation import native_tool  # noqa: PLC0415

    def memory_read_session_summary(
        target_session_id: str,
        scope: str = "session",
        user_intent: str = "",
    ) -> dict[str, Any]:
        return _summary_result(agent_def, target_session_id, scope, user_intent)

    return native_tool(
        memory_read_session_summary,
        name="memory_read_session_summary",
        presentation="memory",
        title="Read session summary",
        representation="row",
        desc="Read a bounded summary of one retained session without returning its full transcript.",
        args={
            "target_session_id": {"type": "string", "description": "Session to summarize."},
            "scope": {"type": "string", "description": "Requested memory scope."},
            "user_intent": {
                "type": "string",
                "description": "The user's reason for a cross-session read.",
            },
        },
    )


def build_memory_context_frame_tool(agent_def: Any) -> Any:
    """Build the bounded retained-context reader."""

    from clio_agent.gact.agents.tool_instrumentation import native_tool  # noqa: PLC0415

    def memory_read_context_frame(
        target_session_id: str,
        frame_id: str,
        scope: str = "session",
        user_intent: str = "",
    ) -> dict[str, Any]:
        return _frame_result(agent_def, target_session_id, frame_id, scope, user_intent)

    return native_tool(
        memory_read_context_frame,
        name="memory_read_context_frame",
        presentation="memory",
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
    )


def build_memory_tools(agent_def: Any) -> list[Any]:
    """Build all retained-memory tools in stable prompt order."""

    return [
        build_memory_search_tool(agent_def),
        build_memory_summary_tool(agent_def),
        build_memory_context_frame_tool(agent_def),
    ]


__all__ = [
    "build_memory_context_frame_tool",
    "build_memory_search_tool",
    "build_memory_summary_tool",
    "build_memory_tools",
]
