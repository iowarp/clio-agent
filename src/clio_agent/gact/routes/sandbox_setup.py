"""Desktop protected-execution ("sandbox") row + setup-trigger routes.

Two loopback-only routes (no extra auth, matching the rest of the GACT ``/v1`` surface — see
``routes/system.py``'s ``/v1/health``) backing the desktop's Infrastructure > Agent "Protected
execution" row:

* ``GET /v1/system/sandbox`` — the ``sandbox`` doctor row alone, projected through the SAME
  :func:`~clio_agent.gact.routes.health_projection.integration_to_wire` helper ``/v1/health``
  uses (no divergent wire shape).
* ``POST /v1/system/sandbox/setup`` — fires the "Set up protected execution" button: runs the
  same ``clio sandbox setup`` provisioning flow
  (:func:`clio_agent.gact.sandbox_setup.run_sandbox_setup`) off the event loop, then returns the
  post-setup verdict plus the re-probed row. Off-Windows there is nothing to provision (typed
  501); a concurrent call while one is already in flight is a typed 409.

New module rather than an addition to ``routes/system.py`` — that file sits near its
``scripts/check_file_size.py`` line-count ratchet, so a new route belongs in its own owner module
(repo CLAUDE.md, no-accretion).
"""

from __future__ import annotations

import asyncio
import logging
import sys
from typing import Any

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from clio_agent.gact.routes.health_projection import integration_to_wire
from clio_agent.gact.sandbox_setup import SandboxSetupConflict, run_sandbox_setup
from clio_agent.runtime import sandbox_cli
from clio_agent.runtime.sandbox_doctor import probe_sandbox

logger = logging.getLogger("clio_agent.gact.routes.sandbox_setup")

#: Typed reason: off-Windows there is nothing to provision (Codex fences automatically via
#: Seatbelt/bubblewrap there) — the setup button is a typed no-op, never a fake elevation.
REASON_SETUP_UNSUPPORTED = "sandbox_setup_unsupported"


def register_sandbox_setup_routes(app: FastAPI) -> None:
    """Register the ``sandbox`` row GET and the setup-trigger POST. Loopback-only, no extra auth."""

    @app.get("/v1/system/sandbox")
    async def get_sandbox_row() -> dict[str, Any]:
        """The ``sandbox`` doctor row alone, wire-projected the same way ``/v1/health`` does."""

        return integration_to_wire(probe_sandbox()).model_dump()

    @app.post("/v1/system/sandbox/setup")
    async def post_sandbox_setup() -> JSONResponse:
        """Run the Codex Windows protected-execution setup and return the post-setup verdict.

        Delegates to :func:`run_sandbox_setup` in a worker thread (``asyncio.to_thread``) — the
        self-elevating flow spawns processes and must never block the event loop. Off-Windows
        this is a typed 501 (nothing to provision); a setup already in flight is a typed 409
        (``sandbox_setup_in_progress``) rather than a second overlapping elevation.
        """

        if not sys.platform.startswith("win"):
            return JSONResponse(
                status_code=501,
                content={
                    "status": sandbox_cli.STATUS_NOT_WINDOWS,
                    "reason": REASON_SETUP_UNSUPPORTED,
                    "elevated": False,
                    "row": integration_to_wire(probe_sandbox()).model_dump(),
                },
            )
        try:
            result = await asyncio.to_thread(run_sandbox_setup)
        except SandboxSetupConflict as exc:
            logger.info("sandbox setup rejected reason=%s", exc.reason)
            return JSONResponse(
                status_code=409,
                content={
                    "status": exc.reason,
                    "reason": exc.reason,
                    "elevated": False,
                    "row": integration_to_wire(probe_sandbox()).model_dump(),
                },
            )
        return JSONResponse(
            content={
                "status": result.status,
                "reason": result.reason,
                "elevated": result.elevated,
                "row": integration_to_wire(result.row).model_dump(),
            }
        )


__all__ = ["REASON_SETUP_UNSUPPORTED", "register_sandbox_setup_routes"]
