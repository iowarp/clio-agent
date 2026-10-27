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
from typing import TYPE_CHECKING, Any

from clio_agent import paths

if TYPE_CHECKING:
    from fastapi import FastAPI


async def reap_orphans(app: FastAPI) -> None:
    """Reap proven orphans of an earlier server and publish the census to health.

    The MCP spawn-cache prune that used to follow is maintenance, not startup (B4): it
    runs at the end of install or upgrade and in ``clio-agent doctor --gc``
    (:func:`clio_agent.tools.mcp_cache.prune_owned_mcp_cache`).
    """
    from clio_agent.gact.routes.system import _prime_orphan_scan_cache  # noqa: PLC0415

    await _prime_orphan_scan_cache(app)


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


async def stop_producers(*tasks: asyncio.Task[Any] | None) -> None:
    """Stop application input producers before task-owner settlement or failed-start exit."""
    pending = [task for task in tasks if task is not None and not task.done()]
    for task in pending:
        task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)


def mark_real_server(app: "FastAPI") -> None:
    """Mark the app ``run_server`` serves: it attaches clio-core and runs the boot doctor
    (``server_boot``), and it owns its process (``owns_process``), so only it may be
    ended by the shutdown budget. Tests that set ``server_boot`` share their process.
    """
    app.state.server_boot = True
    app.state.owns_process = True


async def close_request_owners(app: "FastAPI") -> None:
    """Close the owners that only serve requests, once the loop serves none any more.

    Pending MCP browser sign-ins are released before the document store closes. This
    is the first shutdown step, so it also arms the shutdown budget (shutdown_budget),
    and stops the private SearXNG the server's boot started (search_bootstrap).
    """
    from clio_agent.gact import shutdown_budget  # noqa: PLC0415

    shutdown_budget.begin(app)
    if getattr(app.state, "server_boot", False):
        from clio_agent.gact import search_bootstrap  # noqa: PLC0415

        # The SearXNG this server's boot started stops with it (bounded, typed reasons).
        await search_bootstrap.stop_started(app)
    boot_doctor = getattr(app.state, "health_boot_task", None)
    if boot_doctor is not None:
        boot_doctor.cancel()  # its probe thread ends on its own 1 s timeouts
    sign_ins = getattr(app.state, "mcp_sign_ins", None)
    if sign_ins is not None:
        await sign_ins.close()
    await asyncio.to_thread(app.state.document_store.close)


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
    with _PROCESS_ARC_LOCK:
        arc = getattr(getattr(app, "state", None), "arc", None)
        if arc is None:
            return _construct_process_arc(app)
        from clio_agent.gact.transcript_file import on_process_arc_bound

        on_process_arc_bound(app)
        return arc


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
    from clio_agent.gact import boot_timing  # noqa: PLC0415

    return await boot_timing.timed(app, "clio_core_and_store", asyncio.to_thread(process_arc, app))


async def process_arc_off_loop(app: "FastAPI") -> Any:
    """Return the per-process ARC, constructing it on a worker thread at most once.

    Bind routes can run in an executor's temporary event loop. Dispatch those calls
    onto the app's lifespan loop before touching shared boot tasks. The first caller
    starts the construction task; concurrent callers await
    the same task. ``asyncio.shield`` keeps a cancelled caller (a dropped request)
    from cancelling the shared construction. A failed construction is not cached,
    so the next caller retries.

    Args:
        app: The GACT FastAPI app.

    Returns:
        The ``ARCMemory`` stored on ``app.state.arc``; ``None`` in the loud History mode
        (:mod:`clio_agent.arc.history_mode`), decided once and cached like an ARC.
    """
    owner: asyncio.AbstractEventLoop | None = getattr(app.state, "mcp_app_loop", None)
    if owner is not None and owner is not asyncio.get_running_loop():
        if not owner.is_running():
            raise RuntimeError("The CLIO application event loop is not running")
        pending = asyncio.run_coroutine_threadsafe(process_arc_off_loop(app), owner)
        return await asyncio.wrap_future(pending)

    from clio_agent.gact.sandbox_setup import prepare_sandbox_before_tools  # noqa: PLC0415

    await prepare_sandbox_before_tools(app)
    task: asyncio.Task[Any] | None = getattr(app.state, "arc_boot_task", None)
    if task is not None and not task.done():
        return await asyncio.shield(task)
    arc = getattr(app.state, "arc", None)
    ready = getattr(app.state, "transcript_boot_ready", None)
    if arc is not None and (ready is None or ready.is_set()):
        return arc
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
    from clio_agent.gact import search_bootstrap  # noqa: PLC0415

    search_bootstrap.start(app)  # first-run private SearXNG (search.backend=local_searxng)
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
