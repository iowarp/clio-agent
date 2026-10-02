"""Owner-only GACT server lifecycle routes.

Two owners start a GACT server and must be able to stop it gracefully:

* the desktop shell starts GACT with a one-use bearer token -- exported to the
  process as ``CLIO_AUTH_TOKEN`` and honoured by
  ``gact/auth.py::configured_bearer_token`` -- and marks the process
  desktop-managed via ``CLIO_DESKTOP_MANAGED=1``; its Quit action calls
  ``POST /v1/desktop/shutdown``;
* :func:`clio_agent.serve.ensure_server` does the same for a server it spawns
  (its own one-use ``CLIO_AUTH_TOKEN``, ``CLIO_SERVE_MANAGED=1``), and
  :func:`clio_agent.serve.stop_server` calls ``POST /v1/server/shutdown`` with
  the token the server publishes in its owner-only credential record
  (``gact/server_credentials.py``).

Both routes require that bearer token in the ``Authorization`` header, loopback
included, and are hidden (404) unless the process was started by that owner.
They only SIGNAL shutdown; neither releases the shared clio-core runtime itself
(see ``gact.desktop_lifecycle.request_managed_shutdown``). The single release
happens later, in the app lifespan, right after the turn drain has settled
(``gact.desktop_lifecycle.release_runtime_after_drain``, called from
``gact/app.py``) -- an active turn can no longer reacquire the runtime by then.
``atexit`` (registered in ``arc/storage.py``) stays the backstop for every
other exit path.
"""

from __future__ import annotations

import hmac
import os

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse, Response

from clio_agent.gact import desktop_lifecycle
from clio_agent.gact.auth import _authentication_refusal, _header_bearer_token, has_valid_bearer
from clio_agent.gact.types import ErrorEnvelope, ErrorInfo

DESKTOP_MANAGED_ENV = "CLIO_DESKTOP_MANAGED"
SERVE_MANAGED_ENV = "CLIO_SERVE_MANAGED"


def _unconfigured_refusal(route: str) -> JSONResponse:
    """503: this process has no bearer token to verify the caller against."""

    envelope = ErrorEnvelope(
        error=ErrorInfo(
            error="desktop_lifecycle_unconfigured",
            message=(
                f"{route} is unavailable: this process has no bearer token "
                "configured. Its launcher must export CLIO_AUTH_TOKEN."
            ),
            recoverable=False,
        )
    )
    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        content=envelope.model_dump(exclude_none=True),
    )


def _owner_shutdown(
    app: FastAPI,
    request: Request,
    *,
    managed_env: str,
    owner: desktop_lifecycle.ShutdownOwner,
    route: str,
) -> JSONResponse:
    """Verify the owner's bearer, then signal the graceful shutdown (202)."""

    if os.environ.get(managed_env) != "1":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)

    expected = getattr(app.state, "bearer_token", None)
    if expected is None:
        return _unconfigured_refusal(route)

    supplied = _header_bearer_token(request.scope) or ""
    if not hmac.compare_digest(supplied, expected):
        return _authentication_refusal()

    outcome = desktop_lifecycle.request_managed_shutdown(app, owner)
    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED,
        content={"status": "stopping", "exit_path": outcome.exit_path},
    )


def register_lifecycle_routes(app: FastAPI) -> None:
    """Register the private lifecycle control used by the owning desktop shell."""

    @app.get("/v1/desktop/attach")
    async def desktop_attach_check(request: Request) -> Response:
        """Tell an attaching desktop whether what it presents opens the bearer-only surfaces.

        HTTP from loopback needs no token, so an ordinary request cannot reveal
        whether this server enforces one. The Desktop SSH transport socket and
        desktop shutdown do require it (loopback included). A desktop that
        attaches to a server it did not spawn, and finds no credential record
        (``gact/server_credentials.py``), asks here without a token: 204 means no
        token is enforced and it may proceed; 401 means it cannot authenticate,
        which it reports up front (#1478). Same rule as the transport socket.
        """

        expected = getattr(app.state, "bearer_token", None)
        if expected is None or has_valid_bearer(request.scope, expected):
            return Response(status_code=status.HTTP_204_NO_CONTENT)
        return _authentication_refusal()

    @app.post("/v1/desktop/shutdown", status_code=status.HTTP_202_ACCEPTED)
    async def desktop_shutdown(request: Request) -> JSONResponse:
        return _owner_shutdown(
            app,
            request,
            managed_env=DESKTOP_MANAGED_ENV,
            owner="desktop",
            route="Desktop shutdown",
        )

    @app.post("/v1/server/shutdown", status_code=status.HTTP_202_ACCEPTED)
    async def server_shutdown(request: Request) -> JSONResponse:
        """Graceful stop for a server ``clio_agent.serve`` spawned (``stop_server``)."""

        return _owner_shutdown(
            app,
            request,
            managed_env=SERVE_MANAGED_ENV,
            owner="serve",
            route="Server shutdown",
        )
