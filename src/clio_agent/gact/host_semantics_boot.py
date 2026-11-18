"""Wire host semantics (HS1 to HS5) into the GACT app.

:func:`install` runs at app build: server-owned A2UI surfaces (HS1) and the plugin
tab routes (HS3). :func:`start` and :func:`stop` run with the server's lifespan and
keep plugin account grant files current (HS4). Nothing here names a particular
plugin or server; see ``docs/design/host-semantics-contract.md``.
"""

from __future__ import annotations

import asyncio
from typing import Any


def install(app: Any) -> None:
    """Install the server-surface observer and register the plugin tab routes."""

    from clio_agent.gact.routes.plugin_tabs import register_plugin_tab_routes  # noqa: PLC0415
    from clio_agent.gact.server_surfaces import install_server_surfaces  # noqa: PLC0415

    install_server_surfaces(app)
    register_plugin_tab_routes(app)


def start(app: Any) -> asyncio.Task[None]:
    """Start the account grant file sync on the running loop (HS4)."""

    from clio_agent.gact import account_grant_files  # noqa: PLC0415

    return account_grant_files.start(app)


async def stop(app: Any) -> None:
    """Stop the account grant file sync, if it runs, and wait for it to end."""

    task = getattr(app.state, "account_grants_task", None)
    if task is None or task.done():
        return
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


__all__ = ["install", "start", "stop"]
