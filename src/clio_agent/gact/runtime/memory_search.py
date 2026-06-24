"""Shared transcript-memory search primitives for the GACT server (#714).

CLIO's retained-transcript search powers two distinct surfaces that must score
and excerpt identically:

* the agent-run path -- ``_enrich_with_requested_memory_search`` in
  :mod:`clio_agent.gact.app` injects a requested memory search into a turn's
  context before the LM call; and
* the memory routes -- ``GET /v1/memory/search`` and the agent-callable
  ``POST .../memory/tools/search-sessions`` in
  :mod:`clio_agent.gact.routes.memory`.

Those pure, ``app.state``-reading helpers therefore live here as a leaf module
both surfaces import, keeping a single source of truth for query
normalization, excerpting, and the scope-controlled ranked search. The module
imports only leaf packages (types, ``workspace_scope``, stdlib) and never loads
:mod:`clio_agent.gact.app`.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from fastapi import HTTPException

from clio_agent.gact.types import (
    ErrorEnvelope,
    ErrorInfo,
    MemorySearchHit,
    MemorySearchResponse,
    Message,
)
from clio_agent.gact.workspace_scope import GLOBAL_WORKSPACE_ID, session_scope_label

if TYPE_CHECKING:
    from fastapi import FastAPI


def _memory_search_terms(query: str) -> list[str]:
    """Normalize a memory search query into unique lowercase terms."""

    terms = [term.lower() for term in re.findall(r"[A-Za-z0-9_.@/-]+", query)]
    seen: set[str] = set()
    deduped: list[str] = []
    for term in terms:
        if term in seen:
            continue
        seen.add(term)
        deduped.append(term)
    return deduped


def _memory_search_excerpt(text: str, terms: list[str], *, max_chars: int = 480) -> str:
    """Return a bounded excerpt around the earliest matched term."""

    if len(text) <= max_chars:
        return text
    lowered = text.lower()
    positions = [lowered.find(term) for term in terms if term and lowered.find(term) >= 0]
    center = min(positions) if positions else 0
    start = max(0, center - max_chars // 3)
    end = min(len(text), start + max_chars)
    start = max(0, end - max_chars)
    prefix = "..." if start > 0 else ""
    suffix = "..." if end < len(text) else ""
    return prefix + text[start:end].strip() + suffix


def _message_text_excerpt(message: "Message", *, max_chars: int = 360) -> str:
    """Return a compact, bounded plain-text excerpt of a message's parts.

    Shared by the per-session conversation-history compiler (turn context) and
    the agent-callable session-summary read, so both project a transcript message
    to the same bounded excerpt.
    """

    chunks = [
        part.text.strip()
        for part in message.parts
        if part.type in {"text", "thinking", "error"} and part.text.strip()
    ]
    text = "\n".join(chunks).strip()
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 3].rstrip() + "..."


def _memory_search_response(
    app: "FastAPI",
    *,
    query: str,
    session_id: str = "",
    workspace_id: str = "",
    include_cross_session: bool = False,
    limit: int = 20,
    exclude_message_id: str = "",
) -> MemorySearchResponse:
    """Search retained GACT transcript memory with explicit scope controls.

    Normal calls are session-scoped. Cross-session search is opt-in via
    ``include_cross_session`` and stays within the active workspace (or the
    global workspace) by default; crossing a workspace boundary raises 403.
    """

    terms = _memory_search_terms(query)
    if not terms:
        raise HTTPException(
            status_code=422,
            detail=ErrorEnvelope(
                error=ErrorInfo(
                    error="invalid_request",
                    message="memory search query must contain at least one word",
                    details={"query": query},
                    recoverable=True,
                )
            ).model_dump(exclude_none=True),
        )
    if not include_cross_session and not session_id:
        raise HTTPException(
            status_code=422,
            detail=ErrorEnvelope(
                error=ErrorInfo(
                    error="invalid_request",
                    message="session_id is required unless include_cross_session=true",
                    details={
                        "include_cross_session": include_cross_session,
                        "recovery_actions": [
                            "provide_session_id",
                            "set_include_cross_session",
                        ],
                    },
                    recoverable=True,
                )
            ).model_dump(exclude_none=True),
        )

    limit = max(1, min(int(limit or 20), 100))
    sessions = app.state.sessions.list(workspace_id=workspace_id or None)
    if session_id:
        sess = app.state.sessions.get(session_id)
        if sess is None:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="not_found",
                        message=f"session not found: {session_id}",
                        details={"session_id": session_id},
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )
        if include_cross_session:
            active_workspace_id = workspace_id or str(getattr(sess, "workspace_id", "") or "")
            if workspace_id and sess.workspace_id not in {workspace_id, GLOBAL_WORKSPACE_ID}:
                raise HTTPException(
                    status_code=403,
                    detail=ErrorEnvelope(
                        error=ErrorInfo(
                            error="permission_error",
                            message="memory search cannot cross workspace boundaries by default",
                            details={
                                "session_id": session_id,
                                "session_workspace_id": sess.workspace_id,
                                "requested_workspace_id": workspace_id,
                                "scope": "other_workspace",
                            },
                            recoverable=True,
                        )
                    ).model_dump(exclude_none=True),
                )
            if active_workspace_id and not workspace_id:
                sessions = app.state.sessions.list(workspace_id=active_workspace_id)
            session_ids = [s.id for s in sessions]
            if session_id not in session_ids:
                session_ids.append(session_id)
        else:
            session_ids = [session_id]
    else:
        session_ids = [s.id for s in sessions]

    active_workspace_id = workspace_id
    if not active_workspace_id and session_id:
        active_session = app.state.sessions.get(session_id)
        active_workspace_id = str(getattr(active_session, "workspace_id", "") or "")
    sessions_by_id = {s.id: s for s in app.state.sessions.list()}
    hits: list[MemorySearchHit] = []
    for sid in session_ids:
        sess = sessions_by_id.get(sid)
        if sess is None:
            continue
        for message in app.state.messages.get(sid, []):
            if exclude_message_id and message.id == exclude_message_id:
                continue
            for part in message.parts:
                if part.type not in {"text", "thinking", "error"}:
                    continue
                text = part.text.strip()
                if not text:
                    continue
                lowered = text.lower()
                matched = [term for term in terms if term in lowered]
                if not matched:
                    continue
                score = len(set(matched)) / len(set(terms))
                scope_label = session_scope_label(
                    active_workspace_id=active_workspace_id,
                    target_workspace_id=sess.workspace_id,
                    target_session_id=sid,
                    active_session_id=session_id,
                )
                hits.append(
                    MemorySearchHit(
                        session_id=sid,
                        session_title=sess.title,
                        workspace_id=sess.workspace_id,
                        message_id=message.id,
                        part_id=part.id,
                        role=message.role,
                        created_at=message.created_at,
                        updated_at=message.updated_at,
                        text=_memory_search_excerpt(text, matched),
                        score=round(score, 4),
                        match_terms=sorted(set(matched)),
                        metadata={
                            "cross_session": sid != session_id,
                            "source": "gact_transcript",
                            "scope": scope_label,
                            "workspace_boundary": scope_label,
                        },
                    )
                )

    hits.sort(key=lambda hit: (hit.score, hit.created_at), reverse=True)
    return MemorySearchResponse(
        query=query,
        include_cross_session=include_cross_session,
        searched_sessions=session_ids,
        hits=hits[:limit],
        metadata={
            "scope": "cross_session" if include_cross_session else "session",
            "workspace_id": active_workspace_id,
            "workspace_scope": "global"
            if active_workspace_id == GLOBAL_WORKSPACE_ID
            else "workspace",
            "limit": limit,
        },
    )


__all__ = [
    "_memory_search_terms",
    "_memory_search_excerpt",
    "_message_text_excerpt",
    "_memory_search_response",
]
