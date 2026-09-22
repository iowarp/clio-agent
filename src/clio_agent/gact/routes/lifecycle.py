"""Desktop-owned GACT server lifecycle routes.

The desktop shell starts GACT with a one-use bearer token -- exported to the
process as ``CLIO_AUTH_TOKEN`` and honoured by
``gact/auth.py::configured_bearer_token`` -- and marks the process
desktop-managed via ``CLIO_DESKTOP_MANAGED=1``. Its Quit action calls the
route below, authenticated with that same token, to let uvicorn unwind
normally.

The route only SIGNALS shutdown; it never releases the shared clio-core
runtime itself (see ``gact.desktop_lifecycle.request_desktop_shutdown``). The
single release happens later, in the app lifespan, right after the turn
drain has settled (``gact.desktop_lifecycle.release_runtime_after_drain``,
called from ``gact/app.py``) -- an active turn can no longer reacquire the
runtime by then. ``atexit`` (registered in ``arc/storage.py``) stays the
backstop for every other exit path.
"""

from __future__ import annotations

import hmac
import os

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse

from clio_agent.gact import desktop_lifecycle
from clio_agent.gact.auth import _authentication_refusal, _header_bearer_token
from clio_agent.gact.types import ErrorEnvelope, ErrorInfo

DESKTOP_MANAGED_ENV = "CLIO_DESKTOP_MANAGED"


def _unconfigured_refusal() -> JSONResponse:
    """503: this process has no bearer token to verify the caller against."""

    envelope = ErrorEnvelope(
        error=ErrorInfo(
            error="desktop_lifecycle_unconfigured",
            message=(
                "Desktop shutdown is unavailable: this process has no bearer token "
                "configured. The desktop launcher must export CLIO_AUTH_TOKEN."
            ),
            recoverable=False,
        )
    )
    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        content=envelope.model_dump(exclude_none=True),
    )


def register_lifecycle_routes(app: FastAPI) -> None:
    """Register the private lifecycle control used by the owning desktop shell."""

    @app.post("/v1/desktop/shutdown", status_code=status.HTTP_202_ACCEPTED)
    async def desktop_shutdown(request: Request) -> JSONResponse:
        if os.environ.get(DESKTOP_MANAGED_ENV) != "1":
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)

        expected = getattr(app.state, "bearer_token", None)
        if expected is None:
            return _unconfigured_refusal()

        supplied = _header_bearer_token(request.scope) or ""
        if not hmac.compare_digest(supplied, expected):
            return _authentication_refusal()

        outcome = desktop_lifecycle.request_desktop_shutdown(app)
        return JSONResponse(
            status_code=status.HTTP_202_ACCEPTED,
            content={"status": "stopping", "exit_path": outcome.exit_path},
        )
