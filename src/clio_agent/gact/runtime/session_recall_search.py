"""Shared session recall search primitives for the GACT server (#714).

CLIO's retained-transcript search powers surfaces that must score and excerpt
identically: the recall routes -- ``GET /v1/session-recall/search`` and the
agent-callable ``POST .../session-recall/search`` in
:mod:`clio_agent.gact.routes.session_recall` -- and the search assistant.

Those pure, ``app.state``-reading helpers therefore live here as a leaf module
both surfaces import, keeping a single source of truth for query
normalization, excerpting, and the scope-controlled ranked search. The module
imports only leaf packages (types, ``workspace_scope``, stdlib) and never loads
:mod:`clio_agent.gact.app`.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import TYPE_CHECKING

from fastapi import HTTPException

from clio_agent.gact.attention.rendered import _project
from clio_agent.gact.transcript_reads import read_transcript, transcript_may_contain
from clio_agent.gact.types import (
    ErrorEnvelope,
    ErrorInfo,
    Message,
    SessionRecallHit,
    SessionRecallResponse,
)
from clio_agent.gact.workspace_scope import GLOBAL_WORKSPACE_ID, session_scope_label

if TYPE_CHECKING:
    from fastapi import FastAPI


def _recall_search_terms(query: str) -> list[str]:
    """Normalize a memory search query into unique lowercase terms."""

    terms = [term.lower() for term in re.findall(r"[\w.@/-]+", query, re.UNICODE)]
    seen: set[str] = set()
    deduped: list[str] = []
    for term in terms:
        if term in seen:
            continue
        seen.add(term)
        deduped.append(term)
    return deduped


def _recall_search_phrase(text: str, query: str) -> tuple[int, int] | None:
    """Find pasted rendered prose even when the stored Markdown contains emphasis or links."""
    if not query.strip():
        return None
    raw = re.search(r"\s+".join(re.escape(word) for word in query.split()), text, re.I)
    if raw:
        return raw.span()
    rendered, offsets = _project(text, source=True)
    needle, _ = _project(query, source=False)
    found = re.search(re.escape(needle), rendered, re.I) if needle else None
    return (offsets[found.start()], offsets[found.end() - 1] + 1) if found else None


def _recall_search_excerpt(
    text: str, terms: list[str], *, max_chars: int = 480, query: str = ""
) -> str:
    """Excerpt the exact phrase, otherwise the densest cluster of matching terms."""

    if len(text) <= max_chars:
        return text
    lowered = text.lower()
    phrase = _recall_search_phrase(text, query)
    positions = sorted(
        [
            (match.start(), term)
            for term in terms
            if term
            for match in re.finditer(re.escape(term), lowered)
        ]
    )
    # A common word near the start must not hide a useful match much later.
    center = phrase[0] if phrase else 0
    counts: Counter[str] = Counter()
    left = best = 0
    if not phrase:
        for position, term in positions:
            counts[term] += 1
            while position - positions[left][0] >= max_chars // 2:
                counts[positions[left][1]] -= 1
                if not counts[positions[left][1]]:
                    del counts[positions[left][1]]
                left += 1
            if len(counts) > best:
                best, center = len(counts), positions[left][0]
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


def agent_session_search(
    app: "FastAPI", session_id: str, *, query: str, scope: str = "session", limit: int = 20
) -> SessionRecallResponse:
    """Compatibility entry point for task-backed session discovery."""
    from clio_agent.gact.task_transcripts import search_session_alias

    return search_session_alias(app, session_id, query=query, scope=scope, limit=limit)


def _recall_search_response(
    app: "FastAPI",
    *,
    query: str,
    session_id: str = "",
    workspace_id: str = "",
    include_cross_session: bool = False,
    limit: int = 20,
    exclude_message_id: str = "",
    allowed_session_ids: set[str] | None = None,
) -> SessionRecallResponse:
    """Search retained GACT transcript memory with explicit scope controls.

    Normal calls are session-scoped. Cross-session search is opt-in via
    ``include_cross_session`` and stays within the active workspace (or the
    global workspace) by default; crossing a workspace boundary raises 403.
    """

    terms = _recall_search_terms(query)
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
    from clio_agent.gact.session_records import PageQuery  # noqa: PLC0415

    sessions = (
        app.state.sessions.select(PageQuery(workspace_id=workspace_id or None, archived=None))
        if allowed_session_ids is None
        else [
            session
            for sid in sorted(allowed_session_ids)
            if (session := app.state.sessions.get(sid)) is not None
            and (not workspace_id or session.workspace_id == workspace_id)
        ]
    )
    if include_cross_session and not session_id:
        sessions = [
            session
            for session in sessions
            if not (
                isinstance(side := session.metadata.get("side_session"), dict)
                and side.get("ephemeral") is True
            )
        ]
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
                sessions = [row for row in sessions if row.workspace_id == active_workspace_id]
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
    if allowed_session_ids is not None:
        session_ids = [sid for sid in session_ids if sid in allowed_session_ids]
    sessions_by_id = {sid: app.state.sessions.get(sid) for sid in session_ids}
    hits: list[SessionRecallHit] = []
    for sid in session_ids:
        sess = sessions_by_id.get(sid)
        if sess is None or not transcript_may_contain(app, sid, terms):
            continue  # its stored records hold no term: never assembled, never cached
        for message in read_transcript(app, sid):
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
                phrase_match = _recall_search_phrase(text, query) is not None
                if not phrase_match and len(matched) * 2 < len(terms):
                    continue
                score = 1.0 if phrase_match else 0.9 * len(set(matched)) / len(set(terms))
                scope_label = session_scope_label(
                    active_workspace_id=active_workspace_id,
                    target_workspace_id=sess.workspace_id,
                    target_session_id=sid,
                    active_session_id=session_id,
                )
                hits.append(
                    SessionRecallHit(
                        session_id=sid,
                        session_title=sess.title,
                        workspace_id=sess.workspace_id,
                        message_id=message.id,
                        part_id=part.id,
                        role=message.role,
                        created_at=message.created_at,
                        updated_at=message.updated_at,
                        text=_recall_search_excerpt(text, matched, query=query),
                        score=round(score, 4),
                        match_terms=sorted(set(matched)),
                        metadata={
                            "cross_session": sid != session_id,
                            "source": "gact_transcript",
                            "exact_phrase": phrase_match,
                            "scope": scope_label,
                            "workspace_boundary": scope_label,
                        },
                    )
                )

    hits.sort(key=lambda hit: (hit.score, hit.created_at), reverse=True)
    return SessionRecallResponse(
        query=query,
        include_cross_session=include_cross_session,
        searched_sessions=session_ids,
        hits=hits[:limit],
        metadata={
            "scope": "cross_session" if include_cross_session else "session",
            "workspace_id": active_workspace_id,
            "workspace_scope": "global"
            if active_workspace_id == GLOBAL_WORKSPACE_ID
            else "connected_service"
            if not active_workspace_id
            else "workspace",
            "limit": limit,
        },
    )


__all__ = [
    "_recall_search_terms",
    "_recall_search_excerpt",
    "_message_text_excerpt",
    "_recall_search_response",
]
