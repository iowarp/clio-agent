"""Start the provider support restore for a GACT server (owner module).

The restore itself is :mod:`clio_agent.providers.support_restore`; this module
wires it to the server: it plans at startup (so a provider row reports
``support_restoring`` from the first catalog read), runs the installs on a
worker thread with this runtime's installer and component updater, and retires
each restored provider's catalog evidence so the next read shows the provider
as it is now.
"""

from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING, Any

from clio_agent.providers.support_restore import (
    RESTORER,
    RestoreJob,
    RestoreStep,
    plan_boot_restore,
)

if TYPE_CHECKING:
    from fastapi import FastAPI

logger = logging.getLogger(__name__)

#: Typed reason: the startup restore could not even be planned.
PROVIDER_SUPPORT_RESTORE_NOT_PLANNED = "provider_support_restore_not_planned"


def _install(provider_kind: str) -> object:
    from clio_agent.providers.dependencies import ensure_provider_support  # noqa: PLC0415

    return ensure_provider_support(provider_kind)


def _update(provider_kind: str) -> Any:
    from clio_agent.gact.routes.provider_components import (  # noqa: PLC0415
        live_update_environment,
    )
    from clio_agent.providers.components.updater import UPDATER  # noqa: PLC0415

    return UPDATER.run(provider_kind, live_update_environment())


def _retire_catalog_evidence(app: "FastAPI", job: RestoreJob) -> None:
    """Mark every catalog provider of the restored kind for re-discovery (on the app loop)."""
    from clio_agent.gact.provider_catalog_snapshot import invalidate_provider  # noqa: PLC0415
    from clio_agent.providers.catalog import iter_providers  # noqa: PLC0415

    ids = [p.id for p in iter_providers() if p.provider_kind == job.provider_kind]
    loop = getattr(app.state, "mcp_app_loop", None)
    for provider_id in ids:
        if loop is not None and not loop.is_closed():
            loop.call_soon_threadsafe(invalidate_provider, app, provider_id)
        else:
            invalidate_provider(app, provider_id)


def start_restore(app: "FastAPI", steps: list[RestoreStep]) -> list[RestoreJob]:
    """Claim ``steps`` (visible as ``restoring`` at once) and run them on a worker thread.

    Raises:
        RuntimeError: A restore pass is already running.
    """
    jobs = RESTORER.claim(steps)
    if not jobs:
        return []
    thread = threading.Thread(
        target=RESTORER.execute,
        args=(jobs,),
        kwargs={
            "install": _install,
            "update": _update,
            "on_settled": lambda job: _retire_catalog_evidence(app, job),
        },
        name="provider-support-restore",
        daemon=True,
    )
    thread.start()
    app.state.provider_support_restore_thread = thread
    return jobs


def plan_and_start(app: "FastAPI") -> list[RestoreJob]:
    """Plan the restore against the running environment and start it."""
    return start_restore(app, plan_boot_restore())


def start(app: "FastAPI") -> None:
    """Startup entry: restore recorded provider support this environment lacks.

    A planning failure is logged with a typed reason and never blocks startup;
    the providers then report plain ``install_required`` and Install still works.
    """
    try:
        jobs = plan_and_start(app)
    except Exception as exc:  # noqa: BLE001 - startup must continue; the reason is logged typed
        logger.error(
            "⚑ PROVIDER-SUPPORT reason=%s detail=%r", PROVIDER_SUPPORT_RESTORE_NOT_PLANNED, exc
        )
        return
    if jobs:
        logger.info(
            "provider support restore started for %s",
            ", ".join(f"{job.provider_kind}:{job.action}" for job in jobs),
        )


__all__ = ["PROVIDER_SUPPORT_RESTORE_NOT_PLANNED", "plan_and_start", "start", "start_restore"]
