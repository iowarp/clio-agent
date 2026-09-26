"""Server boot work that must never block the event loop (owner module).

Two pieces of startup work used to make a freshly started server unable to answer
``/v1/health`` (ares, 2026-09-25: uvicorn bound 17800, zero health requests served,
``clio start`` gave up after ~90 s):

* **ARC construction** (:func:`clio_agent.gact.runtime.globals._process_arc`) --
  clio-core connect-or-spawn plus a native client attach that can wait tens of
  seconds -- ran inline on the loop inside the deferred agent-construction task.
* **The first doctor collection** is cold (~2-5 s), longer than the launcher's 1 s
  health probe (:mod:`clio_agent.gact.routes.health_boot`).

:func:`start` kicks both off at startup in the real server (``app.state.server_boot``,
set by ``run_server``; in-process test apps never attach to clio-core by side
effect). :func:`process_arc_off_loop` is the single door to the ARC for async
callers: construction runs on a worker thread exactly once per app (single-flight
on the loop), and the deferred agent build or a first ``PUT /v1/providers/lm``
await the same in-flight construction instead of racing a second one. The server
process owns the clio-core attach whether or not an LM provider is configured, so
a headless host still reports ``clio_core_attach: attached``. Progress and failure
are published by the store factory as that typed row
(:mod:`clio_agent.arc.clio_core_attach`).
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from fastapi import FastAPI

logger = logging.getLogger(__name__)


async def _construct(app: "FastAPI") -> Any:
    from clio_agent.gact.runtime.globals import _process_arc  # noqa: PLC0415 - import cycle

    return await asyncio.to_thread(_process_arc, app)


async def process_arc_off_loop(app: "FastAPI") -> Any:
    """Return the per-process ARC, constructing it on a worker thread at most once.

    Must be called on the app's event loop (every caller is a route or a lifespan
    task). The first caller starts the construction task; concurrent callers await
    the same task. ``asyncio.shield`` keeps a cancelled caller (a dropped request)
    from cancelling the shared construction. A failed construction is not cached,
    so the next caller retries.

    Args:
        app: The GACT FastAPI app.

    Returns:
        The ``ARCMemory`` stored on ``app.state.arc``.
    """
    arc = getattr(app.state, "arc", None)
    if arc is not None:
        return arc
    task: asyncio.Task[Any] | None = getattr(app.state, "arc_boot_task", None)
    if task is None or (task.done() and (task.cancelled() or task.exception() is not None)):
        task = asyncio.get_running_loop().create_task(_construct(app))
        app.state.arc_boot_task = task
    return await asyncio.shield(task)


def start(app: "FastAPI") -> None:
    """Start the boot ARC construction and the first doctor collection (real server only).

    A construction failure is logged with its reason here and is also visible on the
    ``clio_core_attach`` row; the next :func:`process_arc_off_loop` caller retries.
    """
    if not getattr(app.state, "server_boot", False):
        return
    from clio_agent.gact.routes import health_boot  # noqa: PLC0415 - routes import this module
    from clio_agent.gact.routes.system import collect_health_report  # noqa: PLC0415

    health_boot.start_boot_collection(app, collect_health_report)
    if getattr(app.state, "arc", None) is not None:
        return

    async def _boot() -> None:
        try:
            await process_arc_off_loop(app)
        except Exception as exc:  # noqa: BLE001 - reported with a typed reason, retried on next use
            logger.error("arc boot construction failed reason=arc_boot_failed error=%r", exc)

    app.state.arc_boot_runner = asyncio.get_running_loop().create_task(_boot())
