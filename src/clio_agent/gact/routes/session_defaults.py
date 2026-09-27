"""Service-owned defaults for newly created agent sessions."""

from __future__ import annotations

from fastapi import FastAPI

from clio_agent.gact.session_defaults import (
    SessionDefaults,
    SessionDefaultsResponse,
    UpdateSessionDefaultsRequest,
)
from clio_agent.gact.spotter_availability import register_spotter_routes


def register_session_defaults_routes(app: FastAPI) -> None:
    """Register read and partial-update routes for session defaults.

    Also registers ``GET /v1/spotter/availability``: the approval-mode pickers
    that choose a session's (or the default) confirmation policy read it to
    disable ``spotter-ai`` with its typed reason before the click.
    """

    register_spotter_routes(app)

    def _respond(value: SessionDefaults) -> SessionDefaultsResponse:
        # Typed load/migration facts (a quarantined file, a cleared legacy
        # effort) travel with the defaults instead of living only in a log.
        return SessionDefaultsResponse(
            **value.model_dump(), degradations=app.state.session_defaults.degradations
        )

    @app.get("/v1/session-defaults", response_model=SessionDefaultsResponse)
    async def get_session_defaults() -> SessionDefaultsResponse:
        return _respond(app.state.session_defaults.get())

    @app.patch("/v1/session-defaults", response_model=SessionDefaultsResponse)
    async def patch_session_defaults(req: UpdateSessionDefaultsRequest) -> SessionDefaultsResponse:
        return _respond(app.state.session_defaults.update(req))
