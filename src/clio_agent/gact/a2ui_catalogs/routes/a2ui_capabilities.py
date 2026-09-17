"""GACT 0.3 per-session A2UI capability-negotiation route (S3).

Sibling of ``a2ui_catalogs.py``'s installed/session catalog-discovery routes:
this ONE route surfaces the same negotiation the server itself uses to pick
a catalog (``gact.a2ui_capabilities.select_catalog``) so a client (or a
support tool) can read "what does the agent support here, what has the
client last told us, and what would selection currently resolve to" without
re-deriving it from the raw session metadata.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException

from clio_agent.gact.a2ui_capabilities import (
    agent_capabilities,
    client_capabilities,
    select_catalog,
)
from clio_agent.gact.types import ErrorEnvelope, ErrorInfo


def _error(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=status,
        detail=ErrorEnvelope(error=ErrorInfo(error=code, message=message)).model_dump(
            exclude_none=True
        ),
    )


def register_a2ui_capabilities_routes(app: FastAPI) -> None:
    """Register ``GET /v1/sessions/{sid}/a2ui/capabilities``."""

    @app.get("/v1/sessions/{sid}/a2ui/capabilities")
    async def get_session_a2ui_capabilities(sid: str) -> dict[str, Any]:
        """Return the agent's, the client's (if remembered), and the selection."""

        if app.state.sessions.get(sid) is None:
            raise _error(404, "not_found", f"session not found: {sid}")
        client = client_capabilities(app, sid)
        # A read of "what would resolve" is not a selection ATTEMPT -- never
        # writes to the S2 ledger (bounded_memory / no-silent-pollution).
        selection = select_catalog(app, sid, record=False)
        return {
            "agent": agent_capabilities(app, sid),
            "client": client.model_dump(mode="json", by_alias=True) if client is not None else None,
            "selection": {
                "catalog_id": selection.catalog_id,
                "reason": selection.reason,
                "client_supported_catalog_ids": list(selection.client_supported_catalog_ids),
                "producible_catalog_ids": list(selection.producible_catalog_ids),
            },
        }


__all__ = ["register_a2ui_capabilities_routes"]
