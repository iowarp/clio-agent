"""Session recall routes for the GACT server (#714): search and read past sessions.

Recall exposes CLIO's retained-transcript surface: a read-only search plus the three
agent-callable, policy-gated recall tools (``session_search``,
``session_read_summary``, ``session_read_context_frame``) the orchestrator uses to
reach across turns/sessions under explicit scope control. Long-term memory (what the
agent keeps about the user) is a different thing under ``/v1/memory``.

* ``GET /v1/session-recall/search`` -- read retained transcript memory. Session-scoped
  by default; cross-session recall is intentionally opt-in
  (``include_cross_session``) and never crosses a workspace boundary by default.
* ``POST /v1/sessions/{sid}/session-recall/search`` -- agent-callable
  bounded transcript search. Peer recall requires trusted Core relationships;
  every call is recorded with its actual session agent identity and provenance.
* ``POST /v1/sessions/{sid}/session-recall/read-summary`` -- agent-callable
  compact session summary (recent excerpts + counts); never returns the raw full
  transcript.
* ``POST /v1/sessions/{sid}/session-recall/read-context-frame`` -- agent-callable
  read of one persisted context frame, bounded and source/policy-stamped.

The ranked-search primitives (``_recall_search_response`` and friends) are shared
with the recall tools and the search assistant, so they live in the leaf
:mod:`clio_agent.gact.runtime.session_recall_search` module -- one source of truth
for scoring/excerpting. The recall-tool error/audit/policy
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
from clio_agent.gact.off_loop import run_off_loop
from clio_agent.gact.runtime.globals import _session_agent_id
from clio_agent.gact.runtime.retention import enforce_list_bound, ledger_guard
from clio_agent.gact.runtime.session_recall_search import (
    _message_text_excerpt,
    _recall_search_response,
)
from clio_agent.gact.types import ErrorEnvelope, ErrorInfo, SessionRecallResponse

if TYPE_CHECKING:
    from clio_agent.gact.routes.deps import GactDeps


def _recall_tool_error(
    *,
    status_code: int,
    error: str,
    message: str,
    details: dict[str, Any],
    recoverable: bool = True,
) -> HTTPException:
    """Build the structured error envelope raised by the session-recall routes."""

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


def _recall_tool_audit(
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
    """Append one provenance-bearing session-recall audit row and publish its event."""

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
    if not hasattr(app.state, "session_recall_audit"):
        app.state.session_recall_audit = []
    app.state.session_recall_audit.append(row)
    enforce_list_bound(
        app, app.state.session_recall_audit, "session_recall_audit", session_id=session_id
    )
    app.state.bus.publish(
        Event(
            type=f"{tool_name}.{'denied' if status == 'denied' else 'completed'}",
            session_id=session_id,
            payload=row,
        )
    )
    return row


def _recall_tool_policy(
    app: FastAPI,
    *,
    session_id: str,
    target_session_id: str,
    body: Mapping[str, Any],
) -> dict[str, Any]:
    """Authorize retained session content through human/configuration Core relationships."""
    from clio_agent.gact.task_access import TaskAccess

    active = app.state.sessions.get(session_id)
    if active is None:
        raise _recall_tool_error(
            status_code=404,
            error="not_found",
            message="Session unavailable",
            details={},
            recoverable=False,
        )
    caller = app.state.conversations.caller(session_id)
    allowed = target_session_id in TaskAccess(app.state.task_history, caller).session_candidates(
        "read"
    )
    target = app.state.sessions.get(target_session_id) if allowed else None
    allowed = allowed and target is not None
    return {
        "allowed": allowed,
        "decision": "allow_core_relationship" if allowed else "deny_session_unavailable",
        "scope": "session" if session_id == target_session_id else "authorized",
        "active_session": active,
        "target_session": target,
        "workspace_id": active.workspace_id,
        "target_workspace_id": target.workspace_id if target is not None else "",
        "user_intent": str(body.get("user_intent") or body.get("reason") or ""),
    }


def _raise_recall_policy_denied(
    app: FastAPI,
    *,
    tool_name: str,
    session_id: str,
    target_session_id: str,
    caller: Mapping[str, Any] | None,
    policy: Mapping[str, Any],
    query: str = "",
) -> None:
    """Record a denied session-recall call in the audit ledger and raise 403."""

    audit = _recall_tool_audit(
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
    raise _recall_tool_error(
        status_code=403,
        error="session_recall_policy_denied",
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


def _recall_session_summary(
    app: FastAPI, session_id: str, *, caller_id: str | None = None
) -> dict[str, Any]:
    """Project a session to a compact summary (recent excerpts + counts).

    Never includes the raw full transcript -- only bounded excerpts of the last
    few messages plus visibility/rollback bookkeeping.
    """

    from clio_agent.gact.task_transcripts import session_activity_messages

    # Agent callers must not learn existence before their read authorization.
    messages = (
        session_activity_messages(app, app.state.conversations.caller(caller_id), session_id)
        if caller_id
        else None
    )
    sess = app.state.sessions.get(session_id)
    if sess is None:
        raise _recall_tool_error(
            status_code=404,
            error="not_found",
            message=f"session not found: {session_id}",
            details={"session_id": session_id},
            recoverable=False,
        )
    if messages is None:
        messages = session_activity_messages(
            app, app.state.conversations.caller(session_id), session_id
        )
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


def register_session_recall_routes(app: FastAPI, deps: "GactDeps") -> None:
    """Register the transcript-memory routes on ``app``.

    Handlers close over the ``app`` argument (FastAPI's decorators need it) and
    reach sessions/messages/context-frames through ``app.state``; this concern
    needs no cross-concern seam from ``deps`` (it is accepted to match the uniform
    ``register_<concern>_routes(app, deps)`` factory signature). The ranked-search
    primitives are imported from :mod:`clio_agent.gact.runtime.session_recall_search`; the
    error/audit/policy + bounded-projection helpers are module-private to this
    concern.
    """

    @app.get("/v1/session-recall/search", response_model=SessionRecallResponse)
    async def memory_search(
        query: str,
        session_id: str = "",
        workspace_id: str = "",
        include_cross_session: bool = False,
        limit: int = 20,
    ) -> SessionRecallResponse:
        """Search retained transcript memory.

        Normal calls are session-scoped. Cross-session search is intentionally
        opt-in so future orchestrator tools can support "based on the last few
        days" without silently leaking unrelated sessions into every turn.
        """

        return await app.state.task_history.io.run(
            _recall_search_response,
            app,
            query=query,
            session_id=session_id,
            workspace_id=workspace_id,
            include_cross_session=include_cross_session,
            limit=limit,
        )

    @app.post("/v1/sessions/{sid}/session-recall/search")
    async def session_recall_search_route(sid: str, request: Request) -> dict[str, Any]:
        """Agent-callable bounded transcript search with explicit memory policy."""

        try:
            body = await request.json()
        except json.JSONDecodeError as exc:
            raise _recall_tool_error(
                status_code=422,
                error="invalid_request",
                message="memory tool request body must be JSON",
                details={"tool_name": "session_search"},
                recoverable=True,
            ) from exc
        if not isinstance(body, Mapping):
            raise _recall_tool_error(
                status_code=422,
                error="invalid_request",
                message="memory tool request body must be an object",
                details={"tool_name": "session_search"},
                recoverable=True,
            )
        active = app.state.sessions.get(sid)
        if active is None:
            raise _recall_tool_error(
                status_code=404,
                error="not_found",
                message=f"session not found: {sid}",
                details={"session_id": sid},
                recoverable=False,
            )
        query = str(body.get("query") or "").strip()
        scope = str(body.get("scope") or "session").strip() or "session"
        limit = max(1, min(int(body.get("limit") or 10), 50))
        user_intent = str(body.get("user_intent") or body.get("reason") or "").strip()
        from clio_agent.gact.runtime.session_recall_search import agent_session_search

        active_workspace = active.workspace_id
        caller_map = {"agent_id": _session_agent_id(active)}
        response = await run_off_loop(
            lambda: agent_session_search(app, sid, query=query, scope=scope, limit=limit)
        )
        policy_decision = response.metadata["policy_decision"]
        policy_scope = response.metadata["policy_scope"]
        audit = _recall_tool_audit(
            app,
            tool_name="session_search",
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
            "tool": "session_search",
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
                    "tool_name": "session_search",
                    "session_id": sid,
                    "workspace_id": active_workspace,
                },
            },
        }

    @app.post("/v1/sessions/{sid}/session-recall/read-summary")
    async def session_recall_summary_route(sid: str, request: Request) -> dict[str, Any]:
        """Agent-callable compact summary read; never returns raw full transcript."""

        try:
            body = await request.json()
        except json.JSONDecodeError as exc:
            raise _recall_tool_error(
                status_code=422,
                error="invalid_request",
                message="memory tool request body must be JSON",
                details={"tool_name": "session_read_summary"},
                recoverable=True,
            ) from exc
        if not isinstance(body, Mapping):
            raise _recall_tool_error(
                status_code=422,
                error="invalid_request",
                message="memory tool request body must be an object",
                details={"tool_name": "session_read_summary"},
                recoverable=True,
            )
        target_sid = str(body.get("target_session_id") or sid).strip() or sid
        policy = _recall_tool_policy(app, session_id=sid, target_session_id=target_sid, body=body)
        caller_map = {"agent_id": _session_agent_id(policy["active_session"])}
        if not policy.get("allowed"):
            _raise_recall_policy_denied(
                app,
                tool_name="session_read_summary",
                session_id=sid,
                target_session_id=target_sid,
                caller=caller_map,
                policy=policy,
            )
        summary = _recall_session_summary(app, target_sid, caller_id=sid)
        audit = _recall_tool_audit(
            app,
            tool_name="session_read_summary",
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
            "tool": "session_read_summary",
            "summary": summary,
            "metadata": {
                "policy_decision": policy.get("decision", ""),
                "policy_scope": policy.get("scope", ""),
                "audit_id": audit["id"],
                "caller": caller_map,
                "provenance": {
                    "source": "gact_memory_tool",
                    "tool_name": "session_read_summary",
                    "session_id": sid,
                    "target_session_id": target_sid,
                },
            },
        }

    @app.post("/v1/sessions/{sid}/session-recall/read-context-frame")
    async def session_recall_frame_route(sid: str, request: Request) -> dict[str, Any]:
        """Agent-callable context-frame read with source/policy provenance."""

        try:
            body = await request.json()
        except json.JSONDecodeError as exc:
            raise _recall_tool_error(
                status_code=422,
                error="invalid_request",
                message="memory tool request body must be JSON",
                details={"tool_name": "session_read_context_frame"},
                recoverable=True,
            ) from exc
        if not isinstance(body, Mapping):
            raise _recall_tool_error(
                status_code=422,
                error="invalid_request",
                message="memory tool request body must be an object",
                details={"tool_name": "session_read_context_frame"},
                recoverable=True,
            )
        target_sid = str(body.get("target_session_id") or sid).strip() or sid
        frame_id = str(body.get("frame_id") or "").strip()
        if not frame_id:
            raise _recall_tool_error(
                status_code=422,
                error="invalid_request",
                message="frame_id is required",
                details={"tool_name": "session_read_context_frame"},
                recoverable=True,
            )
        policy = _recall_tool_policy(app, session_id=sid, target_session_id=target_sid, body=body)
        caller_map = {"agent_id": _session_agent_id(policy["active_session"])}
        if not policy.get("allowed"):
            _raise_recall_policy_denied(
                app,
                tool_name="session_read_context_frame",
                session_id=sid,
                target_session_id=target_sid,
                caller=caller_map,
                policy=policy,
            )
        frame: Mapping[str, Any] | None = None
        with ledger_guard(app):
            frame_rows = list(app.state.context_frames.get(target_sid, []))
        for row in frame_rows:
            if isinstance(row, Mapping) and row.get("id") == frame_id:
                frame = row
                break
        if frame is None:
            raise _recall_tool_error(
                status_code=404,
                error="not_found",
                message=f"context frame not found: {frame_id}",
                details={"session_id": target_sid, "frame_id": frame_id},
                recoverable=False,
            )
        bounded_frame = _bounded_context_frame(frame)
        audit = _recall_tool_audit(
            app,
            tool_name="session_read_context_frame",
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
            "tool": "session_read_context_frame",
            "frame": bounded_frame,
            "metadata": {
                "policy_decision": policy.get("decision", ""),
                "policy_scope": policy.get("scope", ""),
                "audit_id": audit["id"],
                "caller": caller_map,
                "provenance": {
                    "source": "gact_memory_tool",
                    "tool_name": "session_read_context_frame",
                    "session_id": sid,
                    "target_session_id": target_sid,
                    "frame_id": frame_id,
                },
            },
        }
