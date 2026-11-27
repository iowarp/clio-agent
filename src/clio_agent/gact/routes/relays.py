"""Relay routes for CLIO Desktop's quit prompt; see :mod:`clio_agent.gact.relay_quit`.

``GET /v1/relays/quit`` says whether the prompt offers "Also close open relays".
``POST /v1/relays/shutdown`` closes them, bounded, and reports ``relay.shutdown.v1``.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from clio_agent.gact.auth import has_valid_bearer
from clio_agent.gact.relay_quit import (
    DEFAULT_TIMEOUT_S,
    MAX_TIMEOUT_S,
    relay_status,
    shutdown_relays,
)


class RelayShutdownRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    timeout_s: float = Field(default=DEFAULT_TIMEOUT_S, ge=1.0, le=MAX_TIMEOUT_S)


def register_relay_routes(app: FastAPI) -> None:
    """Register the quit prompt's relay status and shutdown routes."""

    @app.get("/v1/relays/quit")
    async def relay_quit_status() -> dict[str, Any]:
        from clio_agent.gact.off_loop import run_off_loop  # noqa: PLC0415

        return await run_off_loop(relay_status, app)

    @app.post("/v1/relays/shutdown")
    async def relay_shutdown(
        request: Request, body: RelayShutdownRequest | None = None
    ) -> dict[str, Any]:
        token = getattr(app.state, "bearer_token", None)
        if token is None or not has_valid_bearer(request.scope, token):
            raise HTTPException(status_code=401, detail="Desktop sign-in required")
        timeout_s = body.timeout_s if body is not None else DEFAULT_TIMEOUT_S
        return await shutdown_relays(app, timeout_s=timeout_s)


__all__ = ["RelayShutdownRequest", "register_relay_routes"]
