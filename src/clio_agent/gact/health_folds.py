"""Health rows folded into ``GET /v1/health`` after the doctor collection (owner module).

Each row reads state the server already holds on ``app.state``: the store migrations
checked at boot, the store-format verdict of the boot attach, and the install of the
shipped default marketplace (the default agents).
"""

from __future__ import annotations

from typing import Any

from clio_agent.gact import store_format_boot, store_migrations
from clio_agent.gact.agent_initialization import AGENT_INIT_FAILED_REASON
from clio_agent.runtime.status import IntegrationState, IntegrationStatus

#: The retry the failed ``default_marketplace`` row offers (``routes/plugins.py``).
DEFAULT_MARKETPLACE_RETRY_PATH = "/v1/plugins/marketplaces/default/retry"


def folded_rows(app: Any) -> list[IntegrationStatus]:
    """The rows ``GET /v1/health`` adds to the doctor collection."""
    return (
        store_migrations.health_rows(app)
        + store_format_boot.health_rows(app)
        + default_marketplace_rows(app)
        + agent_rows(app)
    )


def agent_rows(app: Any) -> list[IntegrationStatus]:
    """The default agent's construction failure, while no agent is live.

    A failed build leaves the server up but every turn refused (``agent_not_available``);
    readiness does not count it as pending, so without this row health read ready.
    The row carries the construction error itself. A successful ``PUT
    /v1/providers/lm`` installs an agent, which retires the row.
    """
    error = str(getattr(app.state, "agent_init_error", "") or "")
    if not error or getattr(app.state, "agent", None) is not None:
        return []
    return [
        IntegrationStatus(
            name="agent",
            state=IntegrationState.DEGRADED,
            summary=f"CLIO's agent could not be built, so messages are refused: {error}",
            config_source="in-process:agent_construct",
            next_action="Choose a model (PUT /v1/providers/lm, or the model picker); that "
            "builds the agent again.",
            details={"agent_init_error": error, "reason": AGENT_INIT_FAILED_REASON},
        )
    ]


def default_marketplace_rows(app: Any) -> list[IntegrationStatus]:
    """Whether the shipped default agents are installed (none before boot installed them).

    Without the default marketplace no conversation can start with the default agent
    (``agent 'clio/standard' is not installed``), so a failed install is a degraded,
    required row carrying its typed reason, never a server that reports itself ready.
    """
    service = getattr(app.state, "plugin_service", None)
    task = getattr(app.state, "plugin_sync_task", None)
    if service is None or task is None or not task.done():
        return []
    error = getattr(service, "ready_error", None)
    if error:
        return [
            IntegrationStatus(
                name="default_marketplace",
                state=IntegrationState.DEGRADED,
                summary="CLIO's default agents are not installed, so conversations cannot "
                f"start with them: {error.get('message', '')}",
                config_source=str(error.get("source", "")),
                next_action="Fix the cause above, then retry the install (POST "
                f"{DEFAULT_MARKETPLACE_RETRY_PATH}); restarting CLIO retries it too.",
                details=dict(error),
            )
        ]
    return [
        IntegrationStatus(
            name="default_marketplace",
            state=IntegrationState.READY,
            summary="CLIO's default agents are installed.",
            config_source="plugin store (default marketplace)",
            next_action="",
        )
    ]


__all__ = ["agent_rows", "default_marketplace_rows", "folded_rows"]
