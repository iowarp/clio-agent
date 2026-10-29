"""First-run web search: install and start CLIO's private SearXNG when it is the backend.

With ``search.backend: local_searxng`` (the default) and
``search.local_searxng.auto_install`` on, the real server's boot installs the managed
``searxng`` service on this computer when it has never been installed, and starts it
when it is installed but not running -- unless the person's last action on it was to
stop, uninstall or delete it. Every step is an ordinary infrastructure operation, so
it is visible (and cancellable) in Infrastructure like one the person started.

A recorded SearXNG whose installation is missing (an install that failed) is not
started: a start can only fail with "install first", once per boot. Boot records
``auto_start_skipped`` on the service record instead, and the person installs it
from Infrastructure.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Literal

from clio_agent.gact.infrastructure.models import (
    AutoStartSkip,
    ServiceActionRequest,
    ServiceRecord,
)
from clio_agent.gact.infrastructure.searxng_service import SERVICE_ID, VARIANT_ID
from clio_agent.search.backend import register_local_endpoint_resolver
from clio_agent.search.settings import (
    SearchConfigurationError,
    SearchSettings,
    load_search_settings,
)

logger = logging.getLogger(__name__)

LOCAL_TARGET = "local"
#: The person's own decisions that auto-start must respect.
_DECLINED = frozenset({"stop", "uninstall", "delete_data"})
_TERMINAL = frozenset({"succeeded", "failed", "cancelled"})
_STARTING = frozenset({"install", "reinstall", "start"})


def register_local_endpoint(app: Any) -> None:
    """Point the search backend at the managed SearXNG this CLIO recorded, if any."""

    def resolve() -> str | None:
        store = getattr(app.state, "infrastructure_store", None)
        record = store.service(LOCAL_TARGET, SERVICE_ID) if store is not None else None
        if record is None or record.state == "not_installed":
            return None
        port = record.configuration.get("port", "").strip()
        return record.connection_url or (f"http://127.0.0.1:{port}" if port else None)

    def phase() -> str | None:
        runtime = getattr(app.state, "infrastructure_runtime", None)
        store = getattr(app.state, "infrastructure_store", None)
        if runtime is not None and _in_flight(runtime):
            return "starting"
        record = store.service(LOCAL_TARGET, SERVICE_ID) if store is not None else None
        if record is not None and _missing_installation(record) is not None:
            return "not_installed"
        if runtime is not None and _last_failed(runtime):
            # A failed install/start is not fixed by "start it": report it unavailable.
            return None
        if record is not None and record.state == "stopped":
            return "stopped"
        return None

    register_local_endpoint_resolver(resolve, phase)


def _missing_installation(record: ServiceRecord) -> str | None:
    """Why the recorded SearXNG is not installed, or ``None`` when it may be."""

    observation = record.observation
    if record.state == "not_installed" or (
        observation is not None and not observation.installed and observation.phase != "installing"
    ):
        error = observation.error if observation is not None else None
        return f"The last installation did not finish ({error})." if error else "Not installed."
    return None


def _last_failed(runtime: Any) -> bool:
    """Whether the newest install/start/stop of the local SearXNG failed."""

    rows = [
        row
        for row in runtime.store.operations()
        if row.service_id == SERVICE_ID
        and row.target_id == LOCAL_TARGET
        and row.action in {*_STARTING, "stop"}
    ]
    rows.sort(key=lambda row: str(row.created_at))
    return bool(rows) and rows[-1].state == "failed"


def _in_flight(runtime: Any) -> bool:
    """Whether an install/start of the local SearXNG is queued or running."""

    return any(
        row.service_id == SERVICE_ID
        and row.target_id == LOCAL_TARGET
        and row.action in _STARTING
        and row.state not in _TERMINAL
        for row in runtime.store.operations()
    )


async def _finish(runtime: Any, operation_id: str, poll_seconds: float) -> str:
    while True:
        row = runtime.store.operation(operation_id)
        if row is not None and row.state in _TERMINAL:
            return str(row.state)
        await asyncio.sleep(poll_seconds)


def _last_action(runtime: Any) -> str:
    """The person's newest lifecycle action on the local SearXNG (status, logs, verify
    ignored). CLIO's own actions (first-run install and start, the stop at shutdown) are
    not the person's decision and never stand for one."""

    rows = [
        row
        for row in runtime.store.operations()
        if row.service_id == SERVICE_ID
        and row.target_id == LOCAL_TARGET
        and row.action in {"install", "reinstall", "start", *_DECLINED}
        and getattr(row, "initiator", "person") != "clio"
    ]
    rows.sort(key=lambda row: str(row.created_at))
    return str(rows[-1].action) if rows else ""


async def _run(
    runtime: Any, action: Literal["install", "start", "stop"], poll_seconds: float
) -> str:
    row = runtime.start_action(
        SERVICE_ID,
        ServiceActionRequest(target_id=LOCAL_TARGET, action=action, variant_id=VARIANT_ID),
        initiator="clio",
    )
    return await _finish(runtime, row.id, poll_seconds)


def _started_by_clio(runtime: Any) -> bool:
    """Whether the newest install or start of the local SearXNG was CLIO's own.

    In any state: a start the person asked for that is still settling is theirs.
    """
    rows = [
        row
        for row in runtime.store.operations()
        if row.service_id == SERVICE_ID
        and row.target_id == LOCAL_TARGET
        and row.action in _STARTING
    ]
    rows.sort(key=lambda row: str(row.created_at))
    return bool(rows) and getattr(rows[-1], "initiator", "person") == "clio"


async def stop_started(app: Any, *, timeout: float = 10.0, poll_seconds: float = 0.5) -> str:
    """At shutdown, stop the local SearXNG when CLIO itself started it.

    A SearXNG the person started from Infrastructure keeps running. The stop is CLIO's
    own action, so the next start of CLIO starts SearXNG again. Returns what happened:
    ``not_started_by_clio``, ``not_running``, ``stopped``, ``stop_failed`` or
    ``stop_timed_out``.
    """

    runtime = getattr(app.state, "infrastructure_runtime", None)
    store = getattr(app.state, "infrastructure_store", None)
    if runtime is None or store is None or not _started_by_clio(runtime):
        return "not_started_by_clio"
    record = store.service(LOCAL_TARGET, SERVICE_ID)
    if record is None or record.state != "running":
        return "not_running"
    try:
        state = await asyncio.wait_for(_run(runtime, "stop", poll_seconds), timeout)
    except TimeoutError:
        logger.warning(
            "local SearXNG not stopped reason=stop_timed_out timeout_s=%s; it keeps running",
            timeout,
        )
        return "stop_timed_out"
    if state != "succeeded":
        logger.warning("local SearXNG not stopped reason=stop_failed state=%s", state)
        return "stop_failed"
    logger.info("local SearXNG stopped with CLIO")
    return "stopped"


async def ensure_local_searxng(
    runtime: Any, settings: SearchSettings | None = None, *, poll_seconds: float = 2.0
) -> str:
    """Install and/or start the local SearXNG when the configuration asks for it.

    Returns what happened: ``not_selected``, ``auto_install_off``, ``declined``,
    ``incompatible``, ``running``, ``not_installed``, ``started``, ``install_failed``
    or ``start_failed``.
    """

    chosen = settings or load_search_settings()
    if chosen.backend != "local_searxng":
        return "not_selected"
    if not chosen.auto_install:
        return "auto_install_off"
    if _last_action(runtime) in _DECLINED:
        return "declined"
    catalog = await runtime.catalog(LOCAL_TARGET)
    service = next((row for row in catalog.services if row.id == SERVICE_ID), None)
    if service is None or not service.variants[0].compatible:
        reason = service.variants[0].reason if service is not None else "not offered"
        logger.warning("local SearXNG not installed reason=incompatible: %s", reason)
        return "incompatible"
    if service.state == "running":
        return "running"
    # ``catalog`` just reconciled the record with what the computer reports.
    record = runtime.store.service(LOCAL_TARGET, SERVICE_ID)
    if record is None:
        logger.info("installing CLIO's private SearXNG (first run, search.backend=local_searxng)")
        if await _run(runtime, "install", poll_seconds) != "succeeded":
            return "install_failed"
    elif (missing := _missing_installation(record)) is not None:
        # Retrying the install every boot would repeat the same failure; the person
        # installs it from Infrastructure, which clears this note.
        logger.warning("local SearXNG not started reason=not_installed: %s", missing)
        runtime.store.update_service(
            LOCAL_TARGET,
            SERVICE_ID,
            state="not_installed",
            auto_start_skipped=AutoStartSkip(reason="not_installed", detail=missing),
        )
        return "not_installed"
    state = await _run(runtime, "start", poll_seconds)
    return "started" if state == "succeeded" else "start_failed"


def start(app: Any) -> None:
    """Boot entry (real server only): run :func:`ensure_local_searxng` in the background."""

    runtime = getattr(app.state, "infrastructure_runtime", None)
    if runtime is None:
        return
    try:
        settings = load_search_settings()
    except SearchConfigurationError as exc:
        logger.error("web search not prepared reason=%s: %s", exc.code, exc)
        return

    async def _prepare() -> None:
        try:
            outcome = await ensure_local_searxng(runtime, settings)
        except (OSError, RuntimeError, ValueError, KeyError) as exc:
            logger.error("local SearXNG preparation failed reason=bootstrap_error: %r", exc)
            return
        logger.info("local SearXNG preparation outcome=%s", outcome)

    app.state.search_bootstrap_task = asyncio.get_running_loop().create_task(_prepare())
