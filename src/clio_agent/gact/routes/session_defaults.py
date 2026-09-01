"""Service-owned defaults for newly created agent sessions."""

from __future__ import annotations

from fastapi import FastAPI

from clio_agent.gact.session_defaults import SessionDefaults, UpdateSessionDefaultsRequest


def register_session_defaults_routes(app: FastAPI) -> None:
    """Register read and partial-update routes for session defaults."""

    @app.get("/v1/session-defaults", response_model=SessionDefaults)
    async def get_session_defaults() -> SessionDefaults:
        return app.state.session_defaults.get()

    @app.patch("/v1/session-defaults", response_model=SessionDefaults)
    async def patch_session_defaults(req: UpdateSessionDefaultsRequest) -> SessionDefaults:
        return app.state.session_defaults.update(req)
