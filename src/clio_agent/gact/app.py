"""GACT v0.2 FastAPI application for CLIO.

Exposes the GACT v0.2 contract surface. Most routes are 501 stubs
today (CLIO-BBBBBBBBBB6); they get wired one at a time in
follow-on iterations (BBB7–BBB12) against the spec at
``gact-tui/contract/SPEC.md`` and the docs in ``docs/tui/``.

Run via::

    clio-agent-gact --host 127.0.0.1 --port 8100

Or::

    uvicorn clio_agent.gact.app:app --host 127.0.0.1 --port 8100

This is a peer of ``clio_agent.ui.api`` (the native CLIO REST API),
not a replacement — both can run side-by-side. The TUI integration
target is the GACT app; existing CLI + direct-Python callers keep
using the native API unchanged.
"""

from __future__ import annotations

import argparse
import time
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from clio_agent.gact.types import (
    AuthInfo,
    BackendInfo,
    Capabilities,
    CapabilityFlags,
    ErrorEnvelope,
    ErrorInfo,
    HealthResponse,
    Integration,
    TransportFlags,
)

# Version pins. Keep in sync with the gact-tui SPEC.md version bump
# history; bump EMULATOR_VERSION-equivalent here only when the
# *module's* behaviour changes, not every spec revision.
CONTRACT_VERSION = "0.2"
GACT_BACKEND_VERSION = "0.1.0"  # version of this clio_agent.gact module


def _not_implemented(capability: str) -> ErrorEnvelope:
    """Build the v0.2 error envelope for a 501 response."""

    return ErrorEnvelope(
        error=ErrorInfo(
            error="config_error",
            message=f"capability not yet implemented: {capability}",
            details={
                "capability": capability,
                "note": (
                    "This endpoint is stubbed at CLIO-BBBBBBBBBB6; it will "
                    "be wired in a follow-on iteration. See "
                    "gact-tui/PLAN.md phase CLIO-BBBBBBBBBB for the roadmap."
                ),
            },
            recoverable=False,
        )
    )


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Startup/shutdown hook.

    Today: records the boot timestamp so ``/v1/health`` can report
    uptime. Future: wire ClioAgent, mount MCP gateway, load config,
    etc. (CLIO-BBBBBBBBBB7+).
    """

    app.state.started_at = time.time()
    yield
    # No-op shutdown for now; ClioAgent.shutdown goes here once
    # wired.


def build_app() -> FastAPI:
    """Construct the FastAPI app.

    Kept as a factory (not a module-level ``app = FastAPI()``) so
    tests can build fresh instances without singleton state; the
    module-level ``app`` below is for ``uvicorn
    clio_agent.gact.app:app`` invocations.
    """

    app = FastAPI(
        title="CLIO GACT v0.2",
        version=GACT_BACKEND_VERSION,
        lifespan=_lifespan,
    )
    # Initialise state eagerly in case the caller skips the lifespan
    # context (TestClient normally runs it, but older FastAPI + some
    # test-utility paths don't).
    app.state.started_at = time.time()

    @app.get("/v1/health", response_model=HealthResponse)
    async def health() -> HealthResponse:
        uptime = int(time.time() - app.state.started_at)
        return HealthResponse(
            healthy=True,
            uptime_s=uptime,
            overall_status="ready",
            integrations=[
                Integration(
                    name="api",
                    status="ready",
                    detail=f"clio-agent-gact {GACT_BACKEND_VERSION}",
                ),
                Integration(
                    name="clio_agent",
                    status="unavailable",
                    detail="ClioAgent wiring deferred to CLIO-BBBBBBBBBB7",
                ),
            ],
        )

    @app.get("/v1/capabilities", response_model=Capabilities)
    async def capabilities() -> Capabilities:
        return Capabilities(
            contract_version=CONTRACT_VERSION,
            backend=BackendInfo(
                name="clio-agent-gact",
                version=GACT_BACKEND_VERSION,
                vendor="iowarp",
                homepage="https://github.com/iowarp/clio-agent",
            ),
            capabilities=CapabilityFlags(
                # v0.1 baseline — everything off until implemented
                # (CLIO-BBBBBBBBBB8 onwards). Honest reporting lets
                # the TUI disable UI for capabilities we don't
                # actually provide.
                sessions=False,
                commands=False,
                metrics=False,
                # v0.2 additions — advertised when the scaffold
                # actually emits them. Turned on piecewise as the
                # follow-on items land.
                agent_routing=False,
                memory=False,
                structured_errors=True,  # always — we return the envelope for every error
                integration_health=True,  # /v1/health above carries it
                tool_telemetry=False,
            ),
            transports=TransportFlags(events_sse=True, events_websocket=False),
            auth=AuthInfo(schemes=["trust_socket"], current="trust_socket"),
        )

    # ---- 501 stubs for the rest of the surface ---------------------------
    # Every route in the v0.2 contract that we haven't wired yet
    # returns the structured error envelope from above. Matches the
    # shape v0.2 clients expect, while honestly reporting that the
    # backend doesn't yet implement the endpoint.

    _stub_routes: list[tuple[str, str, str]] = [
        # (method, path, capability_name_for_error)
        ("GET", "/v1/workspaces", "workspaces"),
        ("GET", "/v1/sessions", "sessions"),
        ("POST", "/v1/sessions", "sessions"),
        ("GET", "/v1/sessions/{sid}", "sessions"),
        ("DELETE", "/v1/sessions/{sid}", "sessions"),
        ("POST", "/v1/sessions/{sid}/messages", "sessions"),
        ("GET", "/v1/sessions/{sid}/messages", "sessions"),
        ("GET", "/v1/sessions/{sid}/events", "sessions"),
        ("GET", "/v1/agents", "agent_routing"),
        ("GET", "/v1/tools", "tools"),
        ("GET", "/v1/commands", "commands"),
        ("GET", "/v1/metrics", "metrics"),
        ("GET", "/v1/memory/stats", "memory"),
    ]

    def _make_stub(cap: str):
        # Use a Request param so FastAPI doesn't try to validate
        # path/query/body params against the handler signature —
        # stubs take anything and return 501.
        async def _stub(request: Request) -> JSONResponse:
            body = _not_implemented(cap).model_dump(exclude_none=True)
            return JSONResponse(status_code=501, content=body)

        return _stub

    for method, path, cap in _stub_routes:
        app.add_api_route(
            path,
            _make_stub(cap),
            methods=[method],
            include_in_schema=False,
        )

    @app.exception_handler(HTTPException)
    async def _http_exception_handler(
        request, exc: HTTPException
    ) -> JSONResponse:
        """Wrap HTTPExceptions in the v0.2 error envelope."""

        if isinstance(exc.detail, dict) and "error" in exc.detail:
            # Already an envelope (caller built one explicitly).
            return JSONResponse(status_code=exc.status_code, content=exc.detail)
        envelope = ErrorEnvelope(
            error=ErrorInfo(
                error="internal_error",
                message=str(exc.detail) if exc.detail else "",
                recoverable=exc.status_code < 500,
            )
        )
        return JSONResponse(
            status_code=exc.status_code,
            content=envelope.model_dump(exclude_none=True),
        )

    return app


# Module-level app for uvicorn-style invocations:
#   uvicorn clio_agent.gact.app:app
app = build_app()


def main() -> None:
    """Console-script entry point.

    Keeps its flag surface narrow on purpose — uvicorn has plenty of
    knobs but most operators just want ``--host`` and ``--port``.
    """

    import uvicorn

    parser = argparse.ArgumentParser(
        prog="clio-agent-gact",
        description="CLIO's GACT v0.2 REST + SSE server.",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8100, type=int)
    parser.add_argument(
        "--reload",
        action="store_true",
        help="auto-reload on source changes (dev only)",
    )
    args = parser.parse_args()
    uvicorn.run(
        "clio_agent.gact.app:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
    )
