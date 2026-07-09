"""Transcript-memory routes for the GACT server (#714).

The "memory" concern exposes CLIO's retained-transcript recall surface: a
read-only search plus the three agent-callable, policy-gated memory tools the
orchestrator uses to reach across turns/sessions under explicit scope control.

* ``GET /v1/memory/search`` -- read retained transcript memory. Session-scoped
  by default; cross-session recall is intentionally opt-in
  (``include_cross_session``) and never crosses a workspace boundary by default.
* ``POST /v1/sessions/{sid}/memory/tools/search-sessions`` -- agent-callable
  bounded transcript search. Same-workspace / global recall require explicit
  user intent (or the matching scope) and every call is recorded in the audit
  ledger with provenance.
* ``POST /v1/sessions/{sid}/memory/tools/read-session-summary`` -- agent-callable
  compact session summary (recent excerpts + counts); never returns the raw full
  transcript.
* ``POST /v1/sessions/{sid}/memory/tools/read-context-frame`` -- agent-callable
  read of one persisted context frame, bounded and source/policy-stamped.

The ranked-search primitives (``_memory_search_response`` and friends) are shared
with the agent-run path in :mod:`clio_agent.gact.app`, so they live in the leaf
:mod:`clio_agent.gact.runtime.memory_search` module both surfaces import -- one
source of truth for scoring/excerpting. The memory-tool error/audit/policy
helpers and the bounded summary/context-frame projections are concern-private and
live here. The module imports only leaf packages (types, events, ``runtime``,
``workspace_scope``, stdlib) and never loads :mod:`clio_agent.gact.app`.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI, HTTPException, Request

from clio_agent.gact.events import Event
from clio_agent.gact.runtime.memory_search import (
    _memory_search_response,
    _message_text_excerpt,
)
from clio_agent.gact.runtime.retention import enforce_list_bound
from clio_agent.gact.types import ErrorEnvelope, ErrorInfo, MemorySearchResponse
from clio_agent.gact.workspace_scope import GLOBAL_WORKSPACE_ID

if TYPE_CHECKING:
    from clio_agent.gact.routes.deps import GactDeps


def _memory_tool_error(
    *,
    status_code: int,
    error: str,
    message: str,
    details: dict[str, Any],
    recoverable: bool = True,
) -> HTTPException:
    """Build the structured error envelope raised by the memory-tool routes."""

    return HTTPException(
        status_code=status_code,
        detail=ErrorEnvelope(
            error=ErrorInfo(
                error=error,
                message=message,
                details=details,
                recoverable=recoverable,
            )
        ).model_dump(exclude_none=True),
    )


def _memory_tool_audit(
    app: FastAPI,
    *,
    tool_name: str,
    session_id: str,
    target_session_id: str = "",
    caller: Mapping[str, Any] | None = None,
    policy_decision: str,
    status: str,
    scope: str,
    query: str = "",
    details: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Append one provenance-bearing memory-tool audit row and publish its event."""

    row = {
        "id": f"memtool_{uuid.uuid4().hex[:12]}",
        "tool_name": tool_name,
        "session_id": session_id,
        "target_session_id": target_session_id,
        "caller": dict(caller or {}),
        "policy_decision": policy_decision,
        "status": status,
        "scope": scope,
        "query": query,
        "details": dict(details or {}),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    if not hasattr(app.state, "memory_tool_audit"):
        app.state.memory_tool_audit = []
    app.state.memory_tool_audit.append(row)
    enforce_list_bound(app, app.state.memory_tool_audit, "memory_tool_audit", session_id=session_id)
    app.state.bus.publish(
        Event(
            type=f"{tool_name}.{'denied' if status == 'denied' else 'completed'}",
            session_id=session_id,
            payload=row,
        )
    )
    return row


def _memory_tool_policy(
    app: FastAPI,
    *,
    session_id: str,
    target_session_id: str,
    body: Mapping[str, Any],
) -> dict[str, Any]:
    """Decide whether a cross-session memory-tool read is permitted.

    Same-session reads are always allowed. Cross-session reads require explicit
    user intent within the same workspace; the global workspace requires global
    intent; any other workspace is denied. Returns a decision dict carrying the
    resolved active/target sessions and scope label.
    """

    active = app.state.sessions.get(session_id)
    if active is None:
        raise _memory_tool_error(
            status_code=404,
            error="not_found",
            message=f"session not found: {session_id}",
            details={"session_id": session_id},
            recoverable=False,
        )
    target = app.state.sessions.get(target_session_id)
    if target is None:
        raise _memory_tool_error(
            status_code=404,
            error="not_found",
            message=f"session not found: {target_session_id}",
            details={"session_id": target_session_id},
            recoverable=False,
        )

    requested_scope = str(body.get("scope") or "session").strip() or "session"
    user_intent = str(body.get("user_intent") or body.get("reason") or "").strip()
    allow_cross_session = bool(body.get("allow_cross_session")) or bool(user_intent)
    allow_global = bool(body.get("allow_global")) or requested_scope in {
        "global",
        "user",
        "user_global",
    }
    active_workspace = str(getattr(active, "workspace_id", "") or "")
    target_workspace = str(getattr(target, "workspace_id", "") or "")

    if session_id == target_session_id:
        return {
            "allowed": True,
            "decision": "allow_same_session",
            "scope": "session",
            "active_session": active,
            "target_session": target,
            "workspace_id": active_workspace,
            "user_intent": user_intent,
        }

    if target_workspace == GLOBAL_WORKSPACE_ID:
        if allow_global:
            return {
                "allowed": True,
                "decision": "allow_global_user_intent",
                "scope": "global",
                "active_session": active,
                "target_session": target,
                "workspace_id": GLOBAL_WORKSPACE_ID,
                "user_intent": user_intent,
            }
        return {
            "allowed": False,
            "decision": "deny_global_requires_intent",
            "scope": "global",
            "active_session": active,
            "target_session": target,
            "workspace_id": GLOBAL_WORKSPACE_ID,
            "user_intent": user_intent,
        }

    if active_workspace and active_workspace == target_workspace:
        if allow_cross_session:
            return {
                "allowed": True,
                "decision": "allow_same_workspace_user_intent",
                "scope": "current_workspace",
                "active_session": active,
                "target_session": target,
                "workspace_id": active_workspace,
                "user_intent": user_intent,
            }
        return {
            "allowed": False,
            "decision": "deny_cross_session_requires_intent",
            "scope": "current_workspace",
            "active_session": active,
            "target_session": target,
            "workspace_id": active_workspace,
            "user_intent": user_intent,
        }

    return {
        "allowed": False,
        "decision": "deny_other_workspace",
        "scope": "other_workspace",
        "active_session": active,
        "target_session": target,
        "workspace_id": active_workspace,
        "target_workspace_id": target_workspace,
        "user_intent": user_intent,
    }


def _raise_memory_policy_denied(
    app: FastAPI,
    *,
    tool_name: str,
    session_id: str,
    target_session_id: str,
    caller: Mapping[str, Any] | None,
    policy: Mapping[str, Any],
    query: str = "",
) -> None:
    """Record a denied memory-tool call in the audit ledger and raise 403."""

    audit = _memory_tool_audit(
        app,
        tool_name=tool_name,
        session_id=session_id,
        target_session_id=target_session_id,
        caller=caller,
        policy_decision=str(policy.get("decision") or "deny"),
        status="denied",
        scope=str(policy.get("scope") or ""),
        query=query,
        details={
            "workspace_id": policy.get("workspace_id", ""),
            "target_workspace_id": policy.get("target_workspace_id", ""),
            "user_intent": policy.get("user_intent", ""),
        },
    )
    raise _memory_tool_error(
        status_code=403,
        error="memory_policy_denied",
        message="memory tool call is outside the permitted session/workspace scope",
        details={
            "tool_name": tool_name,
            "session_id": session_id,
            "target_session_id": target_session_id,
            "scope": policy.get("scope", ""),
            "policy_decision": policy.get("decision", ""),
            "audit_id": audit["id"],
        },
        recoverable=True,
    )


def _memory_session_summary(app: FastAPI, session_id: str) -> dict[str, Any]:
    """Project a session to a compact summary (recent excerpts + counts).

    Never includes the raw full transcript -- only bounded excerpts of the last
    few messages plus visibility/rollback bookkeeping.
    """

    sess = app.state.sessions.get(session_id)
    if sess is None:
        raise _memory_tool_error(
            status_code=404,
            error="not_found",
            message=f"session not found: {session_id}",
            details={"session_id": session_id},
            recoverable=False,
        )
    messages = list(app.state.messages.get(session_id, []))
    excerpts = [
        {
            "message_id": message.id,
            "role": message.role,
            "created_at": message.created_at,
            "excerpt": _message_text_excerpt(message),
        }
        for message in messages[-5:]
        if _message_text_excerpt(message)
    ]
    metadata = getattr(sess, "metadata", {}) or {}
    rollback = metadata.get("last_rollback", {}) if isinstance(metadata, Mapping) else {}
    return {
        "session_id": sess.id,
        "title": sess.title,
        "workspace_id": sess.workspace_id,
        "status": sess.status,
        "created_at": sess.created_at,
        "updated_at": sess.updated_at,
        "message_count": len(messages),
        "visible_message_ids": [message.id for message in messages],
        "recent_excerpts": excerpts,
        "last_rollback": rollback,
        "excluded_message_ids": rollback.get("deleted_message_ids", [])
        if isinstance(rollback, Mapping)
        else [],
        "metadata": {
            "source": "gact_visible_transcript_summary",
            "raw_transcript_included": False,
            "excerpt_limit": len(excerpts),
        },
    }


def _bounded_context_frame(frame: Mapping[str, Any]) -> dict[str, Any]:
    """Project a persisted context frame to a bounded, source-stamped view.

    Caps the number of returned items and never carries raw transcript text.
    """

    items = frame.get("items", [])
    if not isinstance(items, list):
        items = []
    bounded_items = []
    for item in items[:50]:
        if not isinstance(item, Mapping):
            continue
        bounded_items.append(
            {
                "kind": item.get("kind", ""),
                "source_id": item.get("source_id", ""),
                "role": item.get("role", ""),
                "path": item.get("path", ""),
                "display_path": item.get("display_path", ""),
                "included": bool(item.get("included", True)),
                "reason": item.get("reason", ""),
                "tokens_estimated": item.get("tokens_estimated", 0),
                "metadata": item.get("metadata", {}),
            }
        )
    return {
        "id": frame.get("id", ""),
        "session_id": frame.get("session_id", ""),
        "turn_id": frame.get("turn_id", ""),
        "user_message_id": frame.get("user_message_id", ""),
        "assistant_message_id": frame.get("assistant_message_id", ""),
        "created_at": frame.get("created_at", ""),
        "updated_at": frame.get("updated_at", ""),
        "status": frame.get("status", ""),
        "model": frame.get("model", {}),
        "agent": frame.get("agent", {}),
        "prompt": frame.get("prompt", {}),
        "items": bounded_items,
        "tokens_estimated": frame.get("tokens_estimated", 0),
        "metadata": dict(frame.get("metadata", {}) or {})
        | {
            "source": "gact_context_frame",
            "raw_transcript_included": False,
            "items_returned": len(bounded_items),
            "items_total": len(items),
        },
    }


def register_memory_routes(app: FastAPI, deps: "GactDeps") -> None:
    """Register the transcript-memory routes on ``app``.

    Handlers close over the ``app`` argument (FastAPI's decorators need it) and
    reach sessions/messages/context-frames through ``app.state``; this concern
    needs no cross-concern seam from ``deps`` (it is accepted to match the uniform
    ``register_<concern>_routes(app, deps)`` factory signature). The ranked-search
    primitives are imported from :mod:`clio_agent.gact.runtime.memory_search`; the
    error/audit/policy + bounded-projection helpers are module-private to this
    concern.
    """

    @app.get("/v1/memory/search", response_model=MemorySearchResponse)
    async def memory_search(
        query: str,
        session_id: str = "",
        workspace_id: str = "",
        include_cross_session: bool = False,
        limit: int = 20,
    ) -> MemorySearchResponse:
        """Search retained transcript memory.

        Normal calls are session-scoped. Cross-session search is intentionally
        opt-in so future orchestrator tools can support "based on the last few
        days" without silently leaking unrelated sessions into every turn.
        """

        return _memory_search_response(
            app,
            query=query,
            session_id=session_id,
            workspace_id=workspace_id,
            include_cross_session=include_cross_session,
            limit=limit,
        )

    @app.post("/v1/sessions/{sid}/memory/tools/search-sessions")
    async def memory_tool_search_sessions(sid: str, request: Request) -> dict[str, Any]:
        """Agent-callable bounded transcript search with explicit memory policy."""

        try:
            body = await request.json()
        except json.JSONDecodeError as exc:
            raise _memory_tool_error(
                status_code=422,
                error="invalid_request",
                message="memory tool request body must be JSON",
                details={"tool_name": "memory_search_sessions"},
                recoverable=True,
            ) from exc
        if not isinstance(body, Mapping):
            raise _memory_tool_error(
                status_code=422,
                error="invalid_request",
                message="memory tool request body must be an object",
                details={"tool_name": "memory_search_sessions"},
                recoverable=True,
            )
        active = app.state.sessions.get(sid)
        if active is None:
            raise _memory_tool_error(
                status_code=404,
                error="not_found",
                message=f"session not found: {sid}",
                details={"session_id": sid},
                recoverable=False,
            )
        query = str(body.get("query") or "").strip()
        scope = str(body.get("scope") or "session").strip() or "session"
        limit = max(1, min(int(body.get("limit") or 10), 50))
        caller = body.get("caller", {})
        caller_map = caller if isinstance(caller, Mapping) else {}
        user_intent = str(body.get("user_intent") or body.get("reason") or "").strip()
        allow_cross_session = bool(body.get("allow_cross_session")) or bool(user_intent)
        allow_global = bool(body.get("allow_global")) or scope in {
            "global",
            "user",
            "user_global",
        }
        active_workspace = str(getattr(active, "workspace_id", "") or "")

        if scope in {"global", "user", "user_global"}:
            if not allow_global:
                policy = {
                    "decision": "deny_global_requires_intent",
                    "scope": "global",
                    "workspace_id": active_workspace,
                    "target_workspace_id": GLOBAL_WORKSPACE_ID,
                    "user_intent": user_intent,
                }
                _raise_memory_policy_denied(
                    app,
                    tool_name="memory_search_sessions",
                    session_id=sid,
                    target_session_id="",
                    caller=caller_map,
                    policy=policy,
                    query=query,
                )
            response = _memory_search_response(
                app,
                query=query,
                workspace_id=GLOBAL_WORKSPACE_ID,
                include_cross_session=True,
                limit=limit,
            )
            policy_decision = "allow_global_user_intent"
            policy_scope = "global"
        elif scope in {"current_workspace", "workspace", "cross_session"}:
            if not allow_cross_session:
                policy = {
                    "decision": "deny_cross_session_requires_intent",
                    "scope": "current_workspace",
                    "workspace_id": active_workspace,
                    "user_intent": user_intent,
                }
                _raise_memory_policy_denied(
                    app,
                    tool_name="memory_search_sessions",
                    session_id=sid,
                    target_session_id="",
                    caller=caller_map,
                    policy=policy,
                    query=query,
                )
            response = _memory_search_response(
                app,
                query=query,
                session_id=sid,
                workspace_id=active_workspace,
                include_cross_session=True,
                limit=limit,
            )
            policy_decision = "allow_same_workspace_user_intent"
            policy_scope = "current_workspace"
        else:
            response = _memory_search_response(
                app,
                query=query,
                session_id=sid,
                include_cross_session=False,
                limit=limit,
            )
            policy_decision = "allow_same_session"
            policy_scope = "session"

        audit = _memory_tool_audit(
            app,
            tool_name="memory_search_sessions",
            session_id=sid,
            caller=caller_map,
            policy_decision=policy_decision,
            status="completed",
            scope=policy_scope,
            query=query,
            details={
                "searched_sessions": response.searched_sessions,
                "hit_count": len(response.hits),
                "workspace_id": active_workspace,
                "user_intent": user_intent,
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
                "caller": caller_map,
                "provenance": {
                    "source": "gact_memory_tool",
                    "tool_name": "memory_search_sessions",
                    "session_id": sid,
                    "workspace_id": active_workspace,
                },
            },
        }

    @app.post("/v1/sessions/{sid}/memory/tools/read-session-summary")
    async def memory_tool_read_session_summary(sid: str, request: Request) -> dict[str, Any]:
        """Agent-callable compact summary read; never returns raw full transcript."""

        try:
            body = await request.json()
        except json.JSONDecodeError as exc:
            raise _memory_tool_error(
                status_code=422,
                error="invalid_request",
                message="memory tool request body must be JSON",
                details={"tool_name": "memory_read_session_summary"},
                recoverable=True,
            ) from exc
        if not isinstance(body, Mapping):
            raise _memory_tool_error(
                status_code=422,
                error="invalid_request",
                message="memory tool request body must be an object",
                details={"tool_name": "memory_read_session_summary"},
                recoverable=True,
            )
        target_sid = str(body.get("target_session_id") or sid).strip() or sid
        caller = body.get("caller", {})
        caller_map = caller if isinstance(caller, Mapping) else {}
        policy = _memory_tool_policy(app, session_id=sid, target_session_id=target_sid, body=body)
        if not policy.get("allowed"):
            _raise_memory_policy_denied(
                app,
                tool_name="memory_read_session_summary",
                session_id=sid,
                target_session_id=target_sid,
                caller=caller_map,
                policy=policy,
            )
        summary = _memory_session_summary(app, target_sid)
        audit = _memory_tool_audit(
            app,
            tool_name="memory_read_session_summary",
            session_id=sid,
            target_session_id=target_sid,
            caller=caller_map,
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
                "caller": caller_map,
                "provenance": {
                    "source": "gact_memory_tool",
                    "tool_name": "memory_read_session_summary",
                    "session_id": sid,
                    "target_session_id": target_sid,
                },
            },
        }

    @app.post("/v1/sessions/{sid}/memory/tools/read-context-frame")
    async def memory_tool_read_context_frame(sid: str, request: Request) -> dict[str, Any]:
        """Agent-callable context-frame read with source/policy provenance."""

        try:
            body = await request.json()
        except json.JSONDecodeError as exc:
            raise _memory_tool_error(
                status_code=422,
                error="invalid_request",
                message="memory tool request body must be JSON",
                details={"tool_name": "memory_read_context_frame"},
                recoverable=True,
            ) from exc
        if not isinstance(body, Mapping):
            raise _memory_tool_error(
                status_code=422,
                error="invalid_request",
                message="memory tool request body must be an object",
                details={"tool_name": "memory_read_context_frame"},
                recoverable=True,
            )
        target_sid = str(body.get("target_session_id") or sid).strip() or sid
        frame_id = str(body.get("frame_id") or "").strip()
        caller = body.get("caller", {})
        caller_map = caller if isinstance(caller, Mapping) else {}
        if not frame_id:
            raise _memory_tool_error(
                status_code=422,
                error="invalid_request",
                message="frame_id is required",
                details={"tool_name": "memory_read_context_frame"},
                recoverable=True,
            )
        policy = _memory_tool_policy(app, session_id=sid, target_session_id=target_sid, body=body)
        if not policy.get("allowed"):
            _raise_memory_policy_denied(
                app,
                tool_name="memory_read_context_frame",
                session_id=sid,
                target_session_id=target_sid,
                caller=caller_map,
                policy=policy,
            )
        frame: Mapping[str, Any] | None = None
        for row in app.state.context_frames.get(target_sid, []):
            if isinstance(row, Mapping) and row.get("id") == frame_id:
                frame = row
                break
        if frame is None:
            raise _memory_tool_error(
                status_code=404,
                error="not_found",
                message=f"context frame not found: {frame_id}",
                details={"session_id": target_sid, "frame_id": frame_id},
                recoverable=False,
            )
        bounded_frame = _bounded_context_frame(frame)
        audit = _memory_tool_audit(
            app,
            tool_name="memory_read_context_frame",
            session_id=sid,
            target_session_id=target_sid,
            caller=caller_map,
            policy_decision=str(policy.get("decision") or ""),
            status="completed",
            scope=str(policy.get("scope") or ""),
            details={
                "workspace_id": policy.get("workspace_id", ""),
                "target_workspace_id": getattr(policy.get("target_session"), "workspace_id", ""),
                "frame_id": frame_id,
            },
        )
        return {
            "tool": "memory_read_context_frame",
            "frame": bounded_frame,
            "metadata": {
                "policy_decision": policy.get("decision", ""),
                "policy_scope": policy.get("scope", ""),
                "audit_id": audit["id"],
                "caller": caller_map,
                "provenance": {
                    "source": "gact_memory_tool",
                    "tool_name": "memory_read_context_frame",
                    "session_id": sid,
                    "target_session_id": target_sid,
                    "frame_id": frame_id,
                },
            },
        }
