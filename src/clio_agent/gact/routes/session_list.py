"""``GET /v1/sessions``: the conversation list, whole or one page at a time.

Two shapes, chosen by the request (backward compatible):

* **Unpaged** (neither ``limit`` nor ``cursor``): every matching session, newest
  created first, exactly as before paging existed. ``next_cursor``/``total`` are
  absent (v2) or ``null`` (v3). Kept for callers that need the whole set.
* **Paged** (``limit`` and/or ``cursor``): at most ``limit`` sessions (default
  :data:`DEFAULT_PAGE_SIZE`), newest *interaction*
  first (``last_interaction_at``, else ``created_at`` -- the recency the rail
  shows), plus ``next_cursor`` (pass it back as ``cursor`` for the next page;
  ``null`` on the last page) and ``total`` (how many sessions match the filters in
  all pages). A page is read by range from the session table's recent-first
  index; no other session row is read.

Filters apply to both shapes: ``workspace_id`` (default ``ws_default`` unless
``include_all_workspaces``), ``archived`` (default active only), and
``parent_session_id``. ``kinds`` (comma separated ``conversation``, ``branch``,
``agent``, ``lookup``) restricts to those session kinds -- the rail asks for
``conversation,branch`` so flow children never fill its pages.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from clio_agent.gact.off_loop import run_off_loop
from clio_agent.gact.protocol_v3 import project_for_request, session_to_v3
from clio_agent.gact.routes.session_rows import filter_session_rows, rows_to_wire
from clio_agent.gact.session_kind import session_kind
from clio_agent.gact.session_records import InvalidCursorError, PageQuery
from clio_agent.gact.types import ErrorEnvelope, ErrorInfo, ListSessionsResponse

DEFAULT_PAGE_SIZE = 50
SESSION_KINDS = frozenset({"conversation", "branch", "agent", "lookup"})


def _invalid(message: str, **details: Any) -> HTTPException:
    return HTTPException(
        status_code=422,
        detail=ErrorEnvelope(
            error=ErrorInfo(
                error="validation_error", message=message, details=details, recoverable=True
            )
        ).model_dump(exclude_none=True),
    )


def _parse_kinds(kinds: Optional[str]) -> Optional[frozenset[str]]:
    if not kinds:
        return None
    parsed = frozenset(part.strip() for part in kinds.split(",") if part.strip())
    unknown = sorted(parsed - SESSION_KINDS)
    if unknown:
        raise _invalid(
            f"unknown session kind(s): {', '.join(unknown)}",
            kinds=kinds,
            allowed=sorted(SESSION_KINDS),
        )
    return parsed or None


def register_session_list_route(app: FastAPI) -> None:
    """Register ``GET /v1/sessions`` on ``app``."""

    @app.get("/v1/sessions", response_model=ListSessionsResponse)
    async def list_sessions(
        request: Request,
        workspace_id: Optional[str] = None,
        include_all_workspaces: bool = False,
        archived: Optional[bool] = None,
        parent_session_id: Optional[str] = None,
        kinds: Optional[str] = None,
        limit: Optional[int] = None,
        cursor: Optional[str] = None,
    ) -> ListSessionsResponse | JSONResponse:
        """List sessions, whole or one page (see the module docstring)."""

        effective_workspace_id = workspace_id or (None if include_all_workspaces else "ws_default")
        kind_filter = _parse_kinds(kinds)
        store = app.state.sessions
        if limit is None and cursor is None:
            rows = await run_off_loop(lambda: store.list(workspace_id=effective_workspace_id))
            rows = filter_session_rows(rows, archived=archived, parent_session_id=parent_session_id)
            if kind_filter is not None:
                rows = [row for row in rows if session_kind(row) in kind_filter]
            next_cursor: Optional[str] = None
            total: Optional[int] = None
        else:
            if limit is not None and limit <= 0:
                raise _invalid("limit must be a positive integer", limit=limit)
            query = PageQuery(
                workspace_id=effective_workspace_id,
                archived=bool(archived),
                kinds=kind_filter,
                parent_session_id=parent_session_id or None,
                limit=limit or DEFAULT_PAGE_SIZE,
                cursor=cursor,
            )
            try:
                rows, next_cursor, total = await run_off_loop(lambda: store.page(query))
            except InvalidCursorError as exc:
                raise _invalid(str(exc), cursor=cursor, reason=exc.reason) from exc

        def v3() -> JSONResponse:
            return JSONResponse(
                content={
                    "sessions": [session_to_v3(row) for row in rows],
                    "next_cursor": next_cursor,
                    "total": total,
                }
            )

        def v2() -> ListSessionsResponse:
            return ListSessionsResponse(
                sessions=rows_to_wire(rows), next_cursor=next_cursor, total=total
            )

        return project_for_request(request, v3=v3, v2=v2)


__all__ = ["DEFAULT_PAGE_SIZE", "register_session_list_route"]
