"""Server boot work that must never block the event loop (owner module).

Two pieces of startup work used to make a freshly started server unable to answer
``/v1/health`` (ares, 2026-09-25: uvicorn bound 17800, zero health requests served,
``clio start`` gave up after ~90 s):

* **ARC construction** (:func:`process_arc`) --
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
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any

from clio_agent import paths

if TYPE_CHECKING:
    from fastapi import FastAPI


async def prune_orphans_and_cache(app: FastAPI) -> None:
    """Reap proven orphans before inspecting the liveness of cached MCP children."""
    from clio_agent.gact.routes.system import _prime_orphan_scan_cache  # noqa: PLC0415
    from clio_agent.tools.mcp_cache import boot_prune_off_loop  # noqa: PLC0415

    await _prime_orphan_scan_cache(app)
    await boot_prune_off_loop()


def reconcile_connected_storage(app: FastAPI) -> None:
    """Recover interrupted source operations for the server's registered workspaces."""
    storage = getattr(app.state, "connected_storage", None)
    if storage is not None:
        storage.reconcile({row.id: Path(row.root_path) for row in app.state.workspaces.list()})
    from clio_agent.gact.task_recovery import start_task_recovery

    start_task_recovery(app)


async def shutdown_connected_storage(app: FastAPI) -> None:
    """Drain trusted source operations before the server tears down its runtime."""
    storage = getattr(app.state, "connected_storage", None)
    from clio_agent.gact.task_supervisor import task_supervisor

    task_supervisor(app).closing = True
    if storage is not None:
        await storage.shutdown()
        from clio_agent.gact.storage.task_adapter import recover_storage_handles

        recover_storage_handles(app)


async def shutdown_tasks(app: FastAPI) -> None:
    """Settle supervised tasks before the application's sessions and bus close."""
    from clio_agent.gact.task_supervisor import task_supervisor

    await task_supervisor(app).shutdown()


def start_provider_catalog(app: FastAPI) -> asyncio.Task | None:
    """Refresh provider discovery off the request path when enabled for this server."""
    if not getattr(app.state, "refresh_provider_catalog_on_startup", False):
        return None
    from clio_agent.providers.model_discovery.refresh import (  # noqa: PLC0415
        refresh_subscription_catalogs_at_startup,
    )

    task = asyncio.create_task(refresh_subscription_catalogs_at_startup())
    app.state.provider_catalog_startup_task = task
    return task


logger = logging.getLogger(__name__)


_PROCESS_ARC_LOCK = threading.Lock()


def process_arc(app: "FastAPI") -> Any:
    """Return the ONE ARCMemory for this clio-agent, constructing it once on first use.

    ARC is a per-clio-agent keystone: exactly one per process (one ARC per clio-agent,
    N clio-agents per node, one clio-core per node). The gact server OWNS that single
    ARC's lifecycle so that every agent build/bind reuses the SAME instance.

    Stored on ``app.state.arc`` via ``_set_app_arc`` so a single, fail-loud path reaches
    it; rebuilt only if the app has none yet (first build). ``None`` in History mode
    (:mod:`clio_agent.arc.history_mode`): the platform cannot run clio-core.
    """
    arc = getattr(getattr(app, "state", None), "arc", None)
    if arc is not None:
        return arc
    with _PROCESS_ARC_LOCK:  # single-flight: the boot attach and a first event never race
        arc = getattr(getattr(app, "state", None), "arc", None)
        return arc if arc is not None else _construct_process_arc(app)


def arc_for_first_event(app: Any, event_type: str, sid: str) -> Any:
    """The process ARC for an event emitted before any exists.

    Off the event loop it is obtained through the one construction door
    (``server_boot.process_arc``, single-flight with the boot attach). On the loop clio-core
    cannot be attached: a typed failure naming the event, never a bypass of ARC.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return process_arc(app)
    raise RuntimeError(
        f"ARC-as-source: semantic event {event_type!r} (session={sid!r}) was emitted on the "
        "event loop before clio-core is attached; emit it off the loop"
    )


def _construct_process_arc(app: "FastAPI") -> Any:
    from clio_agent.arc import history_mode  # noqa: PLC0415

    mode = history_mode.resolve()
    if mode.is_history:  # the platform has no clio-core: the loud History mode, decided once
        _record_context_mode(app, mode)
        return None
    from clio_agent.arc.memory import ARCMemory  # noqa: PLC0415
    from clio_agent.arc.storage import make_arc_store  # noqa: PLC0415

    data_dir = str(paths.arc_data_dir())
    arc = ARCMemory(data_dir=data_dir, cache_capacity=1000, store=make_arc_store(data_dir=data_dir))
    from clio_agent.gact.runtime.globals import _set_app_arc  # noqa: PLC0415 - import cycle

    _set_app_arc(app, arc)
    _record_context_mode(app, mode)
    from clio_agent.gact.transcript_file import on_process_arc_bound  # noqa: PLC0415

    on_process_arc_bound(app)  # transcript.file off: reconcile + metrics seed from the atoms
    return arc


def _record_context_mode(app: "FastAPI", mode: Any) -> None:
    """Record which context mode this boot runs in (a trace-only boot event, sid ``""``)."""
    from clio_agent.gact.runtime.globals import _emit_semantic_event  # noqa: PLC0415

    summary = (
        f"CLIO runs in History mode ({mode.reason}): context in memory only, nothing durable."
        if mode.is_history
        else "CLIO runs on clio-core."
    )
    _emit_semantic_event(
        app,
        "",
        "context.mode",
        status="completed",
        summary=summary,
        actor={"mechanism": "harness"},
        payload=mode.as_dict(),
    )


async def _construct(app: "FastAPI") -> Any:
    return await asyncio.to_thread(process_arc, app)


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
        The ``ARCMemory`` stored on ``app.state.arc``; ``None`` in the loud History mode
        (:mod:`clio_agent.arc.history_mode`), decided once and cached like an ARC.
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
    """Start the boot ARC construction, first doctor pass and support restore (real server).

    A construction failure is logged with its reason here and is also visible on the
    ``clio_core_attach`` row; the next :func:`process_arc_off_loop` caller retries.
    """
    if not getattr(app.state, "server_boot", False):
        return
    from clio_agent.gact import provider_support_boot  # noqa: PLC0415

    provider_support_boot.start(app)  # recorded provider support a runtime change removed
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
