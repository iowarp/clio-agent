"""First-run web search: install and start CLIO's private SearXNG when it is the backend.

With ``search.backend: local_searxng`` (the default) and
``search.local_searxng.auto_install`` on, the real server's boot installs the managed
``searxng`` service on this computer when it has never been installed, and starts it
when it is installed but not running -- unless the person's last action on it was to
stop, uninstall or delete it. Every step is an ordinary infrastructure operation, so
it is visible (and cancellable) in Infrastructure like one the person started.

A recorded SearXNG whose installation is missing (an install that failed) is installed
again on a later boot, after a backoff that doubles with each consecutive failed
install (:data:`INSTALL_RETRY_BASE_SECONDS` up to :data:`INSTALL_RETRY_MAX_SECONDS`), so
a failure that keeps recurring is not repeated on every boot. While it waits, boot
records ``auto_start_skipped`` on the service record with the next retry time.

CLIO's own installs use the shipped default port unless another process already
listens on it; then they choose the next free loopback port. The install records that
port in the deployment's configuration (which search resolves) and its receipt. A port
the person configured is never moved.
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from clio_agent.gact.infrastructure.models import (
    AutoStartSkip,
    ServiceActionRequest,
    ServiceRecord,
)
from clio_agent.gact.infrastructure.reuse import OWNER_PROCESS_KEY
from clio_agent.gact.infrastructure.searxng_service import SERVICE_ID, VARIANT_ID
from clio_agent.search.backend import register_local_endpoint_resolver
from clio_agent.search.settings import (
    SearchConfigurationError,
    SearchSettings,
    load_search_settings,
)

logger = logging.getLogger(__name__)

LOCAL_TARGET = "local"
#: Delay before boot retries a failed install; doubles per consecutive failure.
INSTALL_RETRY_BASE_SECONDS = 5 * 60
INSTALL_RETRY_MAX_SECONDS = 24 * 60 * 60
#: The shipped ``search.searxng.port``: the only port CLIO moves when it is taken.
DEFAULT_PORT = SearchSettings().port
#: How far past the default port CLIO looks for a free one.
PORT_SEARCH_SPAN = 100
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


def _in_flight_by_clio(runtime: Any) -> list[str]:
    """CLIO's own queued or running installs/starts of the local SearXNG."""

    return [
        row.id
        for row in runtime.store.operations()
        if row.service_id == SERVICE_ID
        and row.target_id == LOCAL_TARGET
        and row.action in _STARTING
        and row.state not in _TERMINAL
        and getattr(row, "initiator", "person") == "clio"
    ]


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
    runtime: Any,
    action: Literal["install", "start", "stop"],
    poll_seconds: float,
    configuration: dict[str, str] | None = None,
) -> str:
    row = runtime.start_action(
        SERVICE_ID,
        ServiceActionRequest(
            target_id=LOCAL_TARGET,
            action=action,
            variant_id=VARIANT_ID,
            configuration=configuration or {},
        ),
        initiator="clio",
    )
    state = await _finish(runtime, row.id, poll_seconds)
    if state != "succeeded":
        # The boot log is where a first run's search failure is diagnosed; say why.
        final = runtime.store.operation(row.id)
        logger.warning(
            "local SearXNG %s %s: %s",
            action,
            state,
            (getattr(final, "error", None) or "no error recorded") if final else "unknown",
        )
    return state


def _port_taken(port: int) -> bool:
    """Whether anything on this host listens on, or holds, loopback ``port``."""

    with socket.socket() as probe:
        probe.settimeout(1)
        if probe.connect_ex(("127.0.0.1", port)) == 0:
            return True
    with socket.socket() as probe:
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            return True
    return False


def _install_port(settings: SearchSettings) -> int:
    """The port CLIO's own install uses: the configured one, or a free one when the
    shipped default is taken by another process."""

    wanted = settings.port
    if wanted != DEFAULT_PORT or not _port_taken(wanted):
        return wanted
    for port in range(wanted + 1, min(wanted + PORT_SEARCH_SPAN, 65535) + 1):
        if not _port_taken(port):
            logger.warning(
                "local SearXNG port %s is taken by another process; installing on port %s",
                wanted,
                port,
            )
            return port
    logger.warning("local SearXNG found no free port near %s; installing on it anyway", wanted)
    return wanted


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _install_retry_at(runtime: Any) -> tuple[int, datetime | None]:
    """Consecutive failed installs of the local SearXNG, and when the next may run.

    A succeeded install ends the streak; a cancelled one (CLIO shut down mid-install)
    neither counts nor ends it.
    """

    failures = 0
    newest: datetime | None = None
    for row in runtime.store.operations():  # newest first
        if (
            row.service_id != SERVICE_ID
            or row.target_id != LOCAL_TARGET
            or row.action not in {"install", "reinstall"}
            or row.state not in {"succeeded", "failed"}
        ):
            continue
        if row.state == "succeeded":
            break
        failures += 1
        if newest is None:
            newest = _parse_time(str(row.updated_at))
    if newest is None:
        return 0, None
    delay = min(INSTALL_RETRY_BASE_SECONDS * 2 ** (failures - 1), INSTALL_RETRY_MAX_SECONDS)
    return failures, newest + timedelta(seconds=delay)


async def _install(runtime: Any, settings: SearchSettings, poll_seconds: float) -> str:
    port = _install_port(settings)
    return await _run(runtime, "install", poll_seconds, {"port": str(port)})


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
    own action, so the next start of CLIO starts SearXNG again. A shutdown that lands
    while CLIO's own install or start is still in flight cancels it and stops what it
    launched: the service worker runs in its own session and would otherwise outlive
    CLIO, and the stop would queue behind the start on the target's lock. Returns what
    happened: ``not_started_by_clio``, ``not_running``, ``stopped``, ``stop_failed`` or
    ``stop_timed_out``.
    """

    boot = getattr(app.state, "search_bootstrap_task", None)
    if boot is not None and not boot.done():
        boot.cancel()  # no further install/start step may follow this stop
    runtime = getattr(app.state, "infrastructure_runtime", None)
    store = getattr(app.state, "infrastructure_store", None)
    if runtime is None or store is None or not _started_by_clio(runtime):
        return "not_started_by_clio"
    in_flight = _in_flight_by_clio(runtime)
    for operation_id in in_flight:
        with suppress(KeyError):
            await runtime.cancel(operation_id)
    record = store.service(LOCAL_TARGET, SERVICE_ID)
    if not in_flight and (record is None or record.state != "running"):
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
    ``incompatible``, ``running``, ``install_deferred``, ``started``, ``install_failed``
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
        if await _install(runtime, chosen, poll_seconds) != "succeeded":
            return "install_failed"
    elif (missing := _missing_installation(record)) is not None:
        failures, retry_at = _install_retry_at(runtime)
        if retry_at is not None and datetime.now(UTC) < retry_at:
            # Retrying on every boot would repeat a recurring failure back to back.
            detail = f"{missing} Retrying after {retry_at.isoformat(timespec='seconds')}."
            logger.warning(
                "local SearXNG install deferred failures=%s retry_at=%s: %s",
                failures,
                retry_at.isoformat(timespec="seconds"),
                missing,
            )
            runtime.store.update_service(
                LOCAL_TARGET,
                SERVICE_ID,
                state="not_installed",
                auto_start_skipped=AutoStartSkip(reason="not_installed", detail=detail),
            )
            return "install_deferred"
        logger.warning(
            "retrying CLIO's private SearXNG install (previous failures=%s): %s",
            failures,
            missing,
        )
        if await _install(runtime, chosen, poll_seconds) != "succeeded":
            return "install_failed"
    # A forced stop of this server skips its graceful search shutdown: bind the
    # service CLIO starts for itself to this process, so it never outlives it.
    owner = {OWNER_PROCESS_KEY: str(os.getpid())}
    state = await _run(runtime, "start", poll_seconds, owner)
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
        # WARNING so the outcome reaches the default boot log, where web search is
        # otherwise silent until it is used.
        logger.warning("local SearXNG preparation outcome=%s", outcome)

    app.state.search_bootstrap_task = asyncio.get_running_loop().create_task(_prepare())
