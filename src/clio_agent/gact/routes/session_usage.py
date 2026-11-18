"""``GET /v1/sessions/{sid}/usage``: model turns, tokens and cost over a session tree.

The totals a script reads as ``RunResult.usage``: the same per-model spend the flow
route's railroad summary reads (:func:`clio_agent.gact.fleet.spend.tree_spend` over the
session and everything it spawned), summed, with the per-model rows beside them.
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import FastAPI, HTTPException

from clio_agent.gact.types import ErrorEnvelope, ErrorInfo


def tree_usage(app: Any, session_id: str) -> dict[str, Any]:
    """Totals and per-model spend over ``session_id``'s tree."""

    from clio_agent.gact.fleet.spend import Spend, session_tree, tree_spend  # noqa: PLC0415

    report = tree_spend(app, session_id)
    total = Spend()
    for spend in report.by_model.values():
        total.merge(spend)
    return {
        "session_id": session_id,
        **total.to_dict(),
        "sessions": len(session_tree(app, session_id)),
        "by_model": {ref: spend.to_dict() for ref, spend in report.by_model.items()},
        "estimated_sessions": len(report.estimated_sessions),
    }


def register_session_usage_route(app: FastAPI) -> None:
    """Register the session-tree usage read."""

    @app.get("/v1/sessions/{sid}/usage")
    async def session_usage(sid: str) -> dict[str, Any]:
        if app.state.sessions.get(sid) is None:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="not_found", message=f"session not found: {sid}", recoverable=False
                    )
                ).model_dump(exclude_none=True),
            )
        # Spend reads every session record in the tree: off the loop.
        return await asyncio.to_thread(tree_usage, app, sid)


__all__ = ["register_session_usage_route", "tree_usage"]
