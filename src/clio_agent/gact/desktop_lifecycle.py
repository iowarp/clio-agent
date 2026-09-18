"""Desktop-facing lifecycle seams for the GACT server process.

Split out of ``gact/app.py`` (file-size ratchet, #775/#774) -- this module
owns the small handful of steps that only matter because a desktop
supervisor process (not a bare terminal) is driving uvicorn's lifetime:

* :func:`serve_foreground` -- the non-reload branch of ``run_server``. It
  stamps ``app.state.uvicorn_server`` so the authenticated desktop lifecycle
  route can set ``should_exit`` for a cross-platform graceful stop (raising
  SIGINT from a request callback is unreliable on Windows).
* :func:`request_desktop_shutdown` -- what the authenticated
  ``POST /v1/desktop/shutdown`` route (``gact/routes/lifecycle.py``) calls
  once the caller's bearer token has been verified. It only SIGNALS shutdown
  (latches the runtime against late reacquisition, wakes blocked provider
  discovery, and schedules uvicorn's exit) -- it never releases the shared
  clio-core runtime itself. Releasing from the request/response cycle risked
  releasing it while an in-flight turn could still reacquire it.
* :func:`release_runtime_after_drain` -- the ONE place the shared clio-core
  runtime is released for a desktop-managed boot: from the gact lifespan,
  right after Desktop Quit's turn drain has settled, before any later
  executor join can block on a provider/tool worker. ``atexit``
  (``arc/storage.py``) stays the backstop for every other exit path.
* :func:`reset_for_boot` / :func:`wake_for_shutdown` -- thin wrappers around
  the runtime shutdown latch and the LM Studio discovery shutdown flag so the
  app lifespan does not import ``arc.storage`` / ``providers.lmstudio_discovery``
  directly at these call sites.
"""

from __future__ import annotations

import asyncio
import logging
import signal
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from fastapi import FastAPI

logger = logging.getLogger(__name__)

_SHUTDOWN_SIGNAL_DELAY_SECONDS = 0.1


@dataclass(frozen=True)
class ShutdownRequestOutcome:
    """How :func:`request_desktop_shutdown` expects this process to exit.

    Reported back over HTTP so the desktop supervisor's own logs corroborate
    which path the server took, without it having to infer that from timing.
    """

    exit_path: Literal["should_exit", "sigint"]


def _request_server_exit(app: "FastAPI") -> None:
    """Ask the owning uvicorn server to unwind, with a signal fallback."""

    server = getattr(app.state, "uvicorn_server", None)
    if server is not None:
        server.should_exit = True
        return
    logger.warning(
        "desktop shutdown could not set uvicorn should_exit "
        "(reason=uvicorn_server_unavailable); falling back to SIGINT"
    )
    signal.raise_signal(signal.SIGINT)


def request_desktop_shutdown(app: "FastAPI") -> ShutdownRequestOutcome:
    """Signal a desktop-managed shutdown; never release the shared runtime.

    Called by the authenticated ``POST /v1/desktop/shutdown`` route once the
    caller's bearer token is verified. Three signals, in order: latch the
    runtime against late reacquisition (synchronous, so it closes the race
    immediately), wake any blocked LM Studio discovery retry so it cannot add
    its window to Desktop Quit, then ask uvicorn to exit after this response
    has had a chance to flush.

    This function does NOT call ``release_runtime_client``. Doing so from the
    request/response cycle was tried and reverted: an active turn could
    immediately reacquire (respawn) the runtime it had just released. The
    single release now happens later, in the app lifespan, once the turn
    drain has made that reacquisition impossible (see
    :func:`release_runtime_after_drain`).

    Args:
        app: The FastAPI app instance driving this desktop-managed process.

    Returns:
        The exit path this process is expected to take, for the route's
        response body.
    """
    from clio_agent.arc import storage  # noqa: PLC0415
    from clio_agent.providers.lmstudio_discovery import (  # noqa: PLC0415
        request_discovery_shutdown,
    )

    app.state.desktop_shutdown_requested = True
    # This synchronous flag write closes late-reacquisition races immediately;
    # the potentially slower last-client daemon stop stays off the event loop
    # (it happens later, in release_runtime_after_drain).
    storage.prepare_runtime_shutdown()
    request_discovery_shutdown()

    exit_path: Literal["should_exit", "sigint"] = (
        "should_exit" if getattr(app.state, "uvicorn_server", None) is not None else "sigint"
    )
    loop = asyncio.get_running_loop()
    loop.call_later(_SHUTDOWN_SIGNAL_DELAY_SECONDS, _request_server_exit, app)
    return ShutdownRequestOutcome(exit_path=exit_path)


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
        logger.info("desktop.runtime_release outcome=not_desktop")
        return "not_desktop"

    from clio_agent.arc.storage import release_runtime_client  # noqa: PLC0415

    await asyncio.to_thread(release_runtime_client)
    logger.info("desktop.runtime_release outcome=released")
    return "released"


def reset_for_boot() -> None:
    """Clear any prior runtime-shutdown latch + LM Studio discovery shutdown flag.

    Runs at lifespan boot. Both flags are process-global rather than
    lifespan-scoped, so without this reset a second ``build_app``/lifespan
    boot inside the SAME interpreter (a desktop relaunch that reuses the
    process, or a test harness building a second app) could never reacquire
    the shared clio-core runtime again after the first Quit latched it.
    """
    from clio_agent.arc.storage import reset_runtime_shutdown  # noqa: PLC0415
    from clio_agent.providers.lmstudio_discovery import reset_discovery_shutdown  # noqa: PLC0415

    reset_runtime_shutdown()
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
