"""Desktop-facing lifecycle seams for the GACT server process.

Split out of ``gact/app.py`` (file-size ratchet, #775/#774) -- this module
owns the small handful of steps that only matter because a desktop
supervisor process (not a bare terminal) is driving uvicorn's lifetime:

* :func:`serve_foreground` -- the non-reload branch of ``run_server``. It
  stamps ``app.state.uvicorn_server`` so the authenticated desktop lifecycle
  route can set ``should_exit`` for a cross-platform graceful stop (raising
  SIGINT from a request callback is unreliable on Windows).
* :func:`release_runtime_after_drain` -- releases the shared clio-core
  runtime once Desktop Quit's turn drain has settled, before any later
  executor join can block on a provider/tool worker.
* :func:`reset_for_boot` / :func:`wake_for_shutdown` -- thin wrappers around
  the LM Studio discovery shutdown flag so the app lifespan does not import
  ``providers.lmstudio_discovery`` directly at two call sites.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from fastapi import FastAPI

logger = logging.getLogger(__name__)


def serve_foreground(app: "FastAPI", *, host: str, port: int) -> None:
    """Run ``app`` in the foreground via uvicorn until it is told to stop.

    Keeps the concrete server reachable by the authenticated desktop
    lifecycle route. Raising SIGINT from a request callback is unreliable on
    Windows: the HTTP response succeeds, but uvicorn can continue serving
    until the desktop's fallback kills Python and thereby skips shared-
    runtime cleanup. Setting ``should_exit`` is uvicorn's direct,
    cross-platform graceful-stop contract, so ``app.state.uvicorn_server``
    must be stamped before ``server.run()`` blocks.

    Args:
        app: The built GACT FastAPI app to serve.
        host: Bind host.
        port: Bind port.
    """
    import uvicorn

    server = uvicorn.Server(uvicorn.Config(app, host=host, port=port))
    app.state.uvicorn_server = server
    server.run()


async def release_runtime_after_drain(app: "FastAPI") -> Literal["released", "not_desktop"]:
    """Release the shared clio-core runtime once the desktop turn drain has settled.

    Desktop Quit must release the shared runtime before any later executor join can
    block on a provider/tool worker. The turn drain the caller runs beforehand is the
    safety boundary: cooperative cancellation has been signalled and every asyncio
    turn task has settled or been hard-cancelled, so application work can no longer
    reacquire the runtime. Releasing from the HTTP route itself was too early (an
    active turn could immediately spawn clio-core again); releasing at the very end
    was too late (a stuck executor join let the desktop supervisor kill Python first,
    skipping this cleanup and leaking clio-core).

    Args:
        app: The FastAPI app whose ``state.desktop_shutdown_requested`` flag gates
            the release.

    Returns:
        ``"released"`` if the shared runtime client was released, ``"not_desktop"``
        if this process was never a desktop-driven boot (the flag is unset).
    """
    if not getattr(app.state, "desktop_shutdown_requested", False):
        return "not_desktop"

    from clio_agent.arc.storage import release_runtime_client  # noqa: PLC0415

    await asyncio.to_thread(release_runtime_client)
    return "released"


def reset_for_boot() -> None:
    """Clear any prior LM Studio discovery shutdown flag at lifespan boot."""
    from clio_agent.providers.lmstudio_discovery import reset_discovery_shutdown  # noqa: PLC0415

    reset_discovery_shutdown()


def wake_for_shutdown() -> None:
    """Wake LM Studio provider discovery so it cannot stall shutdown on retries.

    Agent construction runs on an executor thread. Cancelling its asyncio task
    does not stop that thread, and Python waits for executor workers at process
    exit. Waking provider discovery before cancelling the task ensures a missing
    LM Studio instance cannot add its entire retry window to Desktop Quit.
    """
    from clio_agent.providers.lmstudio_discovery import request_discovery_shutdown  # noqa: PLC0415

    request_discovery_shutdown()
