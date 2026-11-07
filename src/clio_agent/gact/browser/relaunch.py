"""Relaunch a profile's browser, keeping its pages (browser addendum B).

Chromium reads the keys it trusts (:func:`certificates.launch_args`) when it starts, so a
certificate the person trusts takes effect when the browser relaunches. Every page keeps
its id, its conversation and its viewers: the agent's handles and the person's Browser
tabs stay valid. Each page reopens at the address it had, its live view restarts on the
new page, and the agent's next result on it carries ``browser_restarted``. The calls
running on the profile's pages finish first: the relaunch takes each page's lock.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING

from clio_agent.gact.browser.settings import page_errors
from clio_agent.runtime import trace

if TYPE_CHECKING:
    from clio_agent.gact.browser.service import BrowserService

__all__ = ["relaunch"]

_WEB = ("http://", "https://")


async def relaunch(service: "BrowserService", profile_name: str) -> int:
    """Relaunch ``profile_name``'s managed browser; how many pages reopened."""
    from clio_agent.gact.browser import live  # noqa: PLC0415

    profile = service.profiles.get(profile_name)
    if profile is None or profile.browser is not None:  # not started, or the person's Chrome
        return 0
    records = sorted(
        (r for r in service.pages.values() if r.profile == profile_name and not r.closed),
        key=lambda r: r.id,
    )
    if not records:  # nothing open: the next page launches it with the new keys
        await service._close_profile(profile)
        return 0
    async with contextlib.AsyncExitStack() as held:
        for record in records:
            await held.enter_async_context(record.lock)
        addresses = {
            r.id: r.main_url if r.url().startswith("chrome-error:") else r.url() for r in records
        }
        for record in records:
            await live.pause_view(record)
            record.relaunching = True
        await service._close_profile(profile)
        fresh = await service.profile(records[0].session_id)
        for record in records:
            await service._bind(record, await fresh.context.new_page())
            record.relaunching = False
            record.restarted = True
            record.certificate = record.load_error = None
            address = addresses[record.id]
            if address.startswith(_WEB):
                try:
                    await record.page.goto(address, wait_until="commit")
                except page_errors() as exc:  # it reports why on the page, as any load
                    trace.event(
                        "BROWSER", "relaunch reload failed page=%s error=%s", record.id, exc
                    )
            await live.resume_view(service, record)
    trace.event("BROWSER", "browser relaunched profile=%s pages=%d", profile_name, len(records))
    return len(records)
