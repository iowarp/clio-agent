"""Desktop-owned GACT server lifecycle routes.

The desktop shell starts GACT with a one-use bearer token and marks that process
as desktop-managed.  Its Quit action uses the authenticated endpoint below to
let uvicorn unwind normally.  That normal exit is load-bearing: Python shutdown
hooks deregister this process from the shared clio-core daemon and stop the
daemon only when no independent CLIO client remains.
"""

from __future__ import annotations

import asyncio
import os
import signal

from fastapi import FastAPI, HTTPException, status

DESKTOP_MANAGED_ENV = "CLIO_DESKTOP_MANAGED"
_SHUTDOWN_SIGNAL_DELAY_SECONDS = 0.1


def _request_server_exit(app: FastAPI) -> None:
    """Ask the owning uvicorn server to unwind, with a signal fallback."""

    server = getattr(app.state, "uvicorn_server", None)
    if server is not None:
        server.should_exit = True
        return
    signal.raise_signal(signal.SIGINT)


async def _release_runtime_then_exit(app: FastAPI) -> None:
    """Release shared ARC ownership before beginning slower app teardown."""

    from clio_agent.arc.storage import release_runtime_client  # noqa: PLC0415

    try:
        await asyncio.to_thread(release_runtime_client)
    finally:
        _request_server_exit(app)


def schedule_desktop_shutdown(app: FastAPI) -> None:
    """Release the runtime, then ask uvicorn to unwind after the response flushes."""

    from clio_agent.arc.storage import prepare_runtime_shutdown  # noqa: PLC0415
    from clio_agent.providers.lmstudio_discovery import (  # noqa: PLC0415
        request_discovery_shutdown,
    )

    # This synchronous flag write closes late-reacquisition races immediately;
    # the potentially slower last-client daemon stop stays off the event loop.
    prepare_runtime_shutdown()
    request_discovery_shutdown()
    loop = asyncio.get_running_loop()

    def _start_shutdown_task() -> None:
        task = loop.create_task(_release_runtime_then_exit(app))
        app.state.desktop_shutdown_task = task

    loop.call_later(
        _SHUTDOWN_SIGNAL_DELAY_SECONDS,
        _start_shutdown_task,
    )


def register_lifecycle_routes(app: FastAPI) -> None:
    """Register the private lifecycle control used by the owning desktop shell."""

    @app.post("/v1/desktop/shutdown", status_code=status.HTTP_202_ACCEPTED)
    async def desktop_shutdown() -> dict[str, str]:
        if os.environ.get(DESKTOP_MANAGED_ENV) != "1":
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
        app.state.desktop_shutdown_requested = True
        schedule_desktop_shutdown(app)
        return {"status": "stopping"}
