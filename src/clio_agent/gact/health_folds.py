"""Health rows folded into ``GET /v1/health`` after the doctor collection (owner module).

Each row reads state the server already holds on ``app.state``: the store migrations
checked at boot, the store-format verdict of the boot attach, and the install of the
shipped default marketplace (the default agents).
"""

from __future__ import annotations

from typing import Any

from clio_agent.gact import store_format_boot, store_migrations
from clio_agent.runtime.status import IntegrationState, IntegrationStatus


def folded_rows(app: Any) -> list[IntegrationStatus]:
    """The rows ``GET /v1/health`` adds to the doctor collection."""
    return (
        store_migrations.health_rows(app)
        + store_format_boot.health_rows(app)
        + default_marketplace_rows(app)
    )


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
                next_action="Fix the cause above, then restart CLIO: it installs the default "
                "agents when it starts.",
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


__all__ = ["default_marketplace_rows", "folded_rows"]
