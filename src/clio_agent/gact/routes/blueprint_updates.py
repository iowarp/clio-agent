"""``GET /v1/agent-blueprints/sources/updates`` -- read-only marketplace checks.

The desktop versions panel wants "is a newer marketplace available" without
a CLIO release and without mutating the source ledger. The comparison logic
(``git ls-remote``/``rev-parse``, typed outcomes) lives in the owner module
:mod:`clio_agent.gact.blueprint_update_check`; this module is a thin FastAPI
registrar, kept separate from ``routes/blueprints.py`` (#775 no-accretion --
that file sits at its file-size ratchet baseline).

Both routes here are literal sub-paths of ``/v1/agent-blueprints/...`` and
MUST be registered before ``register_blueprints_routes`` -- that module ends
in a greedy ``GET /v1/agent-blueprints/{blueprint_id:path}`` catch-all that
would otherwise swallow ``.../sources/updates`` as a blueprint id.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI, HTTPException

from clio_agent.gact.agent_blueprint_sources import (
    load_agent_blueprint_sources as _load_agent_blueprint_sources,
)
from clio_agent.gact.blueprint_update_check import (
    blueprint_source_ls_remote_timeout_s,
    check_all_sources,
    check_source_update,
)
from clio_agent.gact.types import ErrorEnvelope, ErrorInfo

if TYPE_CHECKING:
    from clio_agent.gact.routes.deps import GactDeps


def register_blueprint_updates_routes(app: FastAPI, deps: "GactDeps") -> None:
    """Register the read-only blueprint-source update-check routes on ``app``.

    ``deps`` is accepted (unused) for signature consistency with every other
    ``register_<concern>_routes(app, deps)`` factory in
    :mod:`clio_agent.gact.routes` -- these routes need nothing beyond the
    source ledger + the probe module, both reached as leaf imports.
    """

    @app.get("/v1/agent-blueprints/sources/updates")
    async def list_agent_blueprint_source_updates() -> dict[str, Any]:
        """Probe every registered source's remote head, off the event loop."""

        rows = _load_agent_blueprint_sources()
        timeout_s = blueprint_source_ls_remote_timeout_s()
        statuses = await asyncio.to_thread(check_all_sources, rows, timeout_s=timeout_s)
        return {
            "sources": [status.to_wire() for status in statuses],
            "checked_at": datetime.now(UTC).isoformat(),
        }

    @app.get("/v1/agent-blueprints/sources/{source_id}/updates")
    async def get_agent_blueprint_source_update(source_id: str) -> dict[str, Any]:
        """Probe one registered source's remote head, off the event loop."""

        row = next(
            (r for r in _load_agent_blueprint_sources() if r.get("id") == source_id),
            None,
        )
        if row is None:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="not_found",
                        message=f"agent blueprint source not found: {source_id}",
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )
        timeout_s = blueprint_source_ls_remote_timeout_s()
        status = await asyncio.to_thread(check_source_update, row, timeout_s=timeout_s)
        return {"source": status.to_wire(), "checked_at": datetime.now(UTC).isoformat()}


__all__ = ["register_blueprint_updates_routes"]
