"""Durable semantic trace read route over ARC's canonical ``_events`` log."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any

from fastapi import FastAPI, HTTPException, Query

from clio_agent.gact.semantic_events import semantic_event_from_events_content
from clio_agent.gact.types import ErrorEnvelope, ErrorInfo

if TYPE_CHECKING:
    from clio_agent.gact.routes.deps import GactDeps


def _session_not_found(sid: str) -> HTTPException:
    """Build the shared typed session-not-found response.

    Args:
        sid: Missing session identifier.

    Returns:
        A structured HTTP 404 exception.
    """
    return HTTPException(
        status_code=404,
        detail=ErrorEnvelope(
            error=ErrorInfo(
                error="not_found",
                message=f"session not found: {sid}",
                details={"session_id": sid},
                recoverable=False,
            )
        ).model_dump(exclude_none=True),
    )


def _arc_unavailable(sid: str) -> HTTPException:
    """Build the established typed ARC-unavailable degradation.

    Args:
        sid: Session whose trace could not be read.

    Returns:
        A structured recoverable HTTP 503 exception.
    """
    return HTTPException(
        status_code=503,
        detail=ErrorEnvelope(
            error=ErrorInfo(
                error="arc_unavailable",
                message="ARC memory is not enabled for this deployment",
                details={"session_id": sid},
                recoverable=True,
            )
        ).model_dump(exclude_none=True),
    )


def _in_scope(event_type: str, scope: str) -> bool:
    """Match an event against a dotted semantic namespace.

    Args:
        event_type: Semantic event type from the canonical log.
        scope: Optional exact event type or dotted-prefix namespace.

    Returns:
        Whether the event belongs to the requested scope.
    """
    normalized = scope.strip().rstrip(".")
    return not normalized or event_type == normalized or event_type.startswith(f"{normalized}.")


def register_trace_routes(app: FastAPI, deps: "GactDeps") -> None:
    """Register the ARC-derived semantic trace route.

    Args:
        app: GACT FastAPI application receiving the route.
        deps: Shared route dependencies; unused by this read-only concern.
    """
    del deps

    @app.get("/v1/sessions/{sid}/trace")
    async def get_session_trace(
        sid: str,
        limit: Annotated[int, Query(ge=1, le=2000)] = 500,
        scope: str = "",
    ) -> dict[str, list[dict[str, Any]]]:
        """Return the newest bounded semantic events in oldest-first order.

        Args:
            sid: Session whose canonical ARC log is read.
            limit: Maximum number of latest matching events to return.
            scope: Optional exact or dotted-prefix event-type namespace.

        Returns:
            An events envelope shaped like semantic-event SSE payloads.
        """
        if app.state.sessions.get(sid) is None:
            raise _session_not_found(sid)
        arc = getattr(app.state, "arc", None)
        live = getattr(arc, "_live", None)
        iterator = getattr(live, "iter_session_event_segments", None)
        if arc is None or not callable(iterator):
            raise _arc_unavailable(sid)

        events: list[dict[str, Any]] = []
        for segment in iterator(sid):
            event = semantic_event_from_events_content(
                segment.content,
                session_id=sid,
                turn_id=str(segment.turn_id or ""),
            )
            if _in_scope(event.event_type, scope):
                events.append(event.to_dict())
        return {"events": events[-limit:]}
