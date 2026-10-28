"""Shared session recall search primitives for the GACT server (#714).

CLIO's retained-transcript search powers surfaces that must score and excerpt
identically: the recall routes -- ``GET /v1/session-recall/search`` and the
agent-callable ``POST .../session-recall/search`` in
:mod:`clio_agent.gact.routes.session_recall` -- and the search assistant.

Those pure, ``app.state``-reading helpers therefore live here as a leaf module
both surfaces import, keeping a single source of truth for query
normalization, excerpting, and the scope-controlled ranked search. The module
imports only leaf packages (types, ``workspace_scope``, the recall index, stdlib)
and never loads :mod:`clio_agent.gact.app`.

The ranked search reads each session's search digest from the recall index
(:mod:`clio_agent.gact.recall_index`), never whole transcripts unless a digest is
not current (a typed reason in ``metadata.index``). Work per request is bounded:
at most :func:`recall_session_budget` sessions (the most recently active, with a
typed ``metadata.truncated`` block when sessions were left out), and only the best
``limit`` matches are kept and excerpted while scanning.
"""

from __future__ import annotations

import heapq
import re
from collections import Counter
from typing import TYPE_CHECKING

from fastapi import HTTPException

from clio_agent import conf
from clio_agent.gact.attention.rendered import _project
from clio_agent.gact.recall_digest import SearchPart
from clio_agent.gact.recall_index import DigestReadReport, session_digests
from clio_agent.gact.transcript_reads import transcript_may_contain
from clio_agent.gact.types import (
    ErrorEnvelope,
    ErrorInfo,
    Message,
    SessionRecallHit,
    SessionRecallResponse,
)
from clio_agent.gact.workspace_scope import GLOBAL_WORKSPACE_ID, session_scope_label

if TYPE_CHECKING:
    from collections.abc import Callable

    from fastapi import FastAPI

#: Typed reason a recall search read fewer sessions than were eligible.
SESSION_BUDGET_REASON = "recall_session_budget"


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


def _phrase_matcher(query: str) -> "Callable[[SearchPart], bool]":
    """Whether a part holds ``query`` as a phrase: :func:`_recall_search_phrase` as a
    yes/no, with the query compiled once and the part's rendered text read from its
    digest instead of re-rendered per search."""

    if not query.strip():
        return lambda _part: False
    raw = re.compile(r"\s+".join(re.escape(word) for word in query.split()), re.I)
    needle, _ = _project(query, source=False)
    rendered = re.compile(re.escape(needle), re.I) if needle else None

    def matches(part: SearchPart) -> bool:
        if raw.search(part.text):
            return True
        return rendered is not None and rendered.search(part.rendered_text) is not None

    return matches


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
    sessions = (
        app.state.sessions.list(workspace_id=workspace_id or None)
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
    session_ids, truncated = _within_session_budget(session_ids, sessions_by_id)
    report = DigestReadReport()
    # Bounded: only the best ``limit`` matches are kept while scanning, and only those
    # are excerpted. Digests arrive in no particular order, so a tie on (score,
    # created_at) is broken by transcript position (session order, then part order):
    # the order a stable sort of every match by (score, created_at) descending gives.
    best: list[tuple[float, str, tuple[int, int], tuple[str, SearchPart, list[str], bool]]] = []
    scanned = [sid for sid in session_ids if sessions_by_id.get(sid) is not None]
    position = {sid: index for index, sid in enumerate(scanned)}
    phrase_in = _phrase_matcher(query)
    views = session_digests(
        app, scanned, report, may_contain=lambda sid: transcript_may_contain(app, sid, terms)
    )
    for sid, digest in views:
        for index, part in enumerate(digest.parts):
            if exclude_message_id and part.message_id == exclude_message_id:
                continue
            lowered = part.text.lower()
            matched = [term for term in terms if term in lowered]
            if not matched:
                continue
            phrase_match = phrase_in(part)
            if not phrase_match and len(matched) * 2 < len(terms):
                continue
            score = 1.0 if phrase_match else 0.9 * len(set(matched)) / len(set(terms))
            row = (
                round(score, 4),
                part.created_at,
                (-position[sid], -index),
                (sid, part, matched, phrase_match),
            )
            if len(best) < limit:
                heapq.heappush(best, row)
            elif row > best[0]:
                heapq.heapreplace(best, row)
    hits: list[SessionRecallHit] = []
    for score, _created, _seq, (sid, part, matched, phrase_match) in sorted(best, reverse=True):
        sess = sessions_by_id[sid]
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
                message_id=part.message_id,
                part_id=part.part_id,
                role=part.role,
                created_at=part.created_at,
                updated_at=part.updated_at,
                text=_recall_search_excerpt(part.text, matched, query=query),
                score=score,
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
    metadata: dict[str, object] = {
        "scope": "cross_session" if include_cross_session else "session",
        "workspace_id": active_workspace_id,
        "workspace_scope": "global"
        if active_workspace_id == GLOBAL_WORKSPACE_ID
        else "connected_service"
        if not active_workspace_id
        else "workspace",
        "limit": limit,
        "index": report.as_dict(),
    }
    if truncated:
        metadata["truncated"] = truncated
    return SessionRecallResponse(
        query=query,
        include_cross_session=include_cross_session,
        searched_sessions=session_ids,
        hits=hits,
        metadata=metadata,
    )


def recall_session_budget() -> int:
    """How many sessions one recall search reads at most (the most recent ones).

    Config: ``gact.session_recall.max_sessions`` / ``CLIO_SESSION_RECALL_MAX_SESSIONS``.
    """

    return conf.resolve(
        "gact.session_recall.max_sessions",
        env="CLIO_SESSION_RECALL_MAX_SESSIONS",
        default=5_000,
        cast=conf.as_int,
    )


def _within_session_budget(
    session_ids: list[str], sessions_by_id: dict[str, object]
) -> tuple[list[str], dict[str, object] | None]:
    """The sessions one search reads, in their order, and the typed truncation if any.

    Over budget, the most recently active sessions are kept (the rest are named by
    count in the ``truncated`` block, reason :data:`SESSION_BUDGET_REASON`).
    """

    budget = max(1, recall_session_budget())
    if len(session_ids) <= budget:
        return session_ids, None

    def recency(sid: str) -> str:
        sess = sessions_by_id.get(sid)
        return str(getattr(sess, "last_interaction_at", "") or getattr(sess, "updated_at", ""))

    kept = set(sorted(session_ids, key=recency, reverse=True)[:budget])
    return [sid for sid in session_ids if sid in kept], {
        "reason": SESSION_BUDGET_REASON,
        "searched": budget,
        "eligible": len(session_ids),
        "kept": "most_recently_active",
    }


__all__ = [
    "_recall_search_terms",
    "_recall_search_excerpt",
    "_message_text_excerpt",
    "_recall_search_response",
    "SESSION_BUDGET_REASON",
    "recall_session_budget",
]
