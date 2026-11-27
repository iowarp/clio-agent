"""Wire host semantics (HS1 to HS5) into the GACT app.

:func:`install` runs at app build: server-owned A2UI surfaces (HS1) and the plugin
tab routes (HS3). :func:`start` and :func:`stop` run with the server's lifespan, keep
plugin account grant files current (HS4) and move open sessions onto an updated
plugin (HS3). Nothing here names a particular
plugin or server; see ``docs/design/host-semantics-contract.md``.
"""

from __future__ import annotations

import asyncio
from typing import Any


def install(app: Any) -> None:
    """Install the server-surface observer and the plugin tab, page and resource routes."""

    from clio_agent.gact.routes.plugin_pages import register_plugin_page_routes  # noqa: PLC0415
    from clio_agent.gact.routes.plugin_tabs import register_plugin_tab_routes  # noqa: PLC0415
    from clio_agent.gact.routes.relays import register_relay_routes  # noqa: PLC0415
    from clio_agent.gact.routes.server_resources import (  # noqa: PLC0415
        register_server_resource_routes,
    )
    from clio_agent.gact.server_surfaces import install_server_surfaces  # noqa: PLC0415

    install_server_surfaces(app)
    register_plugin_tab_routes(app)
    register_plugin_page_routes(app)
    register_relay_routes(app)
    register_server_resource_routes(app)


def start(app: Any) -> asyncio.Task[None]:
    """Start the account grant file sync (HS4) and the plugin update follow (HS3)."""

    from clio_agent.gact import account_grant_files, plugin_update_follow  # noqa: PLC0415

    plugin_update_follow.start(app)
    return account_grant_files.start(app)


async def stop(app: Any) -> None:
    """Stop the grant file sync and the plugin update follow, and wait for them to end."""

    resources = getattr(app.state, "server_resources", None)
    if resources is not None:
        resources.shutdown()
    tasks = [
        task
        for task in (
            getattr(app.state, "account_grants_task", None),
            getattr(app.state, "plugin_update_follow_task", None),
        )
        if task is not None and not task.done()
    ]
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


__all__ = ["install", "start", "stop"]
