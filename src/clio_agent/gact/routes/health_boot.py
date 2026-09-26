"""Boot-time doctor collection for ``/v1/health`` (owner module).

The first full doctor collection in a fresh server process is cold: it imports the
tool-server stack to list the gateway and parses every installed Agent Blueprint to
check the declared MCP launchers (~2-5 s on a cold host). ``/v1/health`` must
answer within a second of the port binding whatever that costs (the ``clio start``
launcher probes with a 1 s timeout; ares, 2026-09-25), and a probe that times out
must not leave another cold collection running per retry.

So the real server (``app.state.server_boot``, set by ``run_server``) starts ONE
collection off the loop at startup. While it is in flight, ``/v1/health`` answers
at once with the rows that are cheap and exact right now -- the API itself, the
typed ``clio_core_attach`` state -- plus a ``doctor`` row whose typed reason
(``doctor_boot_collection_in_progress``) says the full rows are still being
collected. Once it finishes (warm caches), every call collects fresh as before.
In-process test apps never set ``server_boot`` and keep the synchronous path.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING

from clio_agent.runtime.status import IntegrationState, IntegrationStatus, RuntimeReport

if TYPE_CHECKING:
    from fastapi import FastAPI

logger = logging.getLogger(__name__)

DOCTOR_BOOT_COLLECTION_IN_PROGRESS = "doctor_boot_collection_in_progress"


def start_boot_collection(app: "FastAPI", collect: Callable[["FastAPI"], RuntimeReport]) -> None:
    """Run the first doctor collection on a worker thread (real server only).

    Args:
        app: The GACT app.
        collect: The health route's own collection function (same probes, same env).
    """
    if not getattr(app.state, "server_boot", False):
        return

    async def _collect() -> None:
        try:
            await asyncio.to_thread(collect, app)
        except Exception as exc:  # noqa: BLE001 - typed log; the next health call collects anew
            logger.warning("health boot collection failed reason=doctor_boot_failed error=%r", exc)

    app.state.health_boot_task = asyncio.get_running_loop().create_task(_collect())


def pending_report(app: "FastAPI") -> RuntimeReport | None:
    """Return the immediate report while the boot collection runs, else ``None``."""
    task = getattr(app.state, "health_boot_task", None)
    if task is None or task.done():
        return None
    from clio_agent.runtime.clio_core_health import probe_clio_core_attach  # noqa: PLC0415

    return RuntimeReport(
        integrations=[
            IntegrationStatus(
                name="api",
                state=IntegrationState.READY,
                summary="CLIO API is serving.",
                config_source="in-process",
                next_action="No action required.",
                required=True,
            ),
            *probe_clio_core_attach(),
            IntegrationStatus(
                name="lm_provider",
                state=IntegrationState.SKIPPED,
                summary="LM provider status is being collected.",
                config_source="in-process:boot-collection",
                next_action="Poll /v1/health again for the provider row.",
                details={"reason": "lm_provider_status_pending"},
                required=False,
            ),
            IntegrationStatus(
                name="doctor",
                state=IntegrationState.DEGRADED,
                summary="Boot-time doctor collection in progress; the full rows follow.",
                config_source="in-process:boot-collection",
                next_action="Poll /v1/health again in a moment.",
                details={"reason": DOCTOR_BOOT_COLLECTION_IN_PROGRESS},
                required=False,
            ),
        ]
    )
