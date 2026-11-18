"""Keep plugin account grant files current (host semantics HS4).

For every provider a registered plugin declares in ``accounts``, the user's grant is
written to that plugin's grant file (:mod:`clio_agent.plugins.account_grants`) and kept
there while the account stays connected: it is written on sign-in, refreshed at least
five minutes before the token expires, and deleted on sign-out or disconnect. A file is
rewritten only when its tokens change, so a server watching it re-reads it only then.
Nothing here names a particular plugin or server.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)

#: Refresh a token this long before it expires. The sync runs every
#: :data:`SYNC_INTERVAL_S`, so a file is rewritten at least five minutes ahead.
REFRESH_LEAD_S = 600.0
SYNC_INTERVAL_S = 60.0


def sync_account_grants(app: Any) -> None:
    """Write, refresh or delete every declared account grant file once (blocking)."""

    from clio_agent.plugins.account_grants import read_grant, remove_grant, write_grant  # noqa: PLC0415
    from clio_agent.plugins.host_semantics import declared_accounts  # noqa: PLC0415

    declared = declared_accounts()
    if not declared:
        return
    service = getattr(app.state, "connected_storage", None)
    for provider, plugins in declared.items():
        rows: list[dict[str, object]] = []
        if service is not None:
            try:
                rows = service.auth.grant_tokens(
                    service.principal,
                    service.store.clio_id,
                    provider,
                    refresh_within=REFRESH_LEAD_S,
                )
            except (PermissionError, ValueError):
                rows = []
            except Exception:  # noqa: BLE001 - one provider must not stop the others
                logger.exception("account grant read failed provider=%s", provider)
                continue
        for plugin in plugins:
            try:
                if not rows:
                    remove_grant(plugin, provider)
                    continue
                current = read_grant(plugin, provider) or {}
                if current.get("tokens") == _comparable(rows):
                    continue
                write_grant(plugin, provider, rows)
            except OSError:
                logger.exception(
                    "account grant file write failed plugin=%s provider=%s", plugin, provider
                )


def _comparable(rows: list[dict[str, object]]) -> list[dict[str, Any]]:
    from clio_agent.plugins.account_grants import grant_document  # noqa: PLC0415

    return grant_document("", rows)["tokens"]


async def _run(app: Any, wake: asyncio.Event) -> None:
    from clio_agent.gact.off_loop import run_off_loop  # noqa: PLC0415

    while True:
        try:
            await run_off_loop(sync_account_grants, app)
        except Exception:  # noqa: BLE001 - the loop outlives one failed pass
            logger.exception("account grant sync failed")
        try:
            await asyncio.wait_for(wake.wait(), timeout=SYNC_INTERVAL_S)
        except TimeoutError:
            pass
        wake.clear()


def start(app: Any) -> asyncio.Task[None]:
    """Start the sync loop on the running loop; sign-in changes wake it at once."""

    loop = asyncio.get_running_loop()
    wake = asyncio.Event()
    service = getattr(app.state, "connected_storage", None)
    auth = getattr(service, "auth", None)
    if auth is not None:
        auth.on_change = lambda: loop.call_soon_threadsafe(wake.set)
    task = asyncio.create_task(_run(app, wake), name="clio-account-grants")
    app.state.account_grants_task = task
    return task


__all__ = ["REFRESH_LEAD_S", "SYNC_INTERVAL_S", "start", "sync_account_grants"]
