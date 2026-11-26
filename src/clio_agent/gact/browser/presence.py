"""A call's target must exist: wait while the page makes progress, fail once it settles.

Playwright's own timeouts are off (its clock cannot pause for the person's host answers), so
a locator call on an element that never attaches would wait forever. Before an element call
runs, :func:`resolve` asks the browser how many elements the target matches. With none, it
keeps asking while the page shows progress: a different address, a document or frame still
loading, a request starting or finishing. When the page has been settled for
:data:`QUIET_S` and still matches nothing, the call fails with ``browser_target_missing``,
naming the target, the page and Playwright's locator. There is no cap on a page that keeps
loading; the turn's Stop ends that wait. Waiting for something that appears later without
loading is ``browser_wait``'s job.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from clio_agent.gact.browser.settings import BrowserError
from clio_agent.gact.browser.targets import describe, locator_for

__all__ = ["QUIET_S", "REASON_MISSING", "resolve"]

REASON_MISSING = "browser_target_missing"
#: Seconds a settled page may match nothing before the call fails.
QUIET_S = 3.0
_POLL_S = 0.25


class _Requests:
    """The page's requests started and settled while a target is awaited."""

    def __init__(self, page: Any) -> None:
        self.page = page
        self.started = 0
        self.settled = 0

    def _start(self, _request: Any) -> None:
        self.started += 1

    def _settle(self, _request: Any) -> None:
        self.settled += 1

    def __enter__(self) -> "_Requests":
        self.page.on("request", self._start)
        self.page.on("requestfinished", self._settle)
        self.page.on("requestfailed", self._settle)
        return self

    def __exit__(self, *_exc: object) -> None:
        self.page.remove_listener("request", self._start)
        self.page.remove_listener("requestfinished", self._settle)
        self.page.remove_listener("requestfailed", self._settle)


async def _ready_states(page: Any) -> tuple[str, ...]:
    states = []
    for frame in page.frames:
        try:
            states.append(await frame.evaluate("document.readyState"))
        except Exception:  # noqa: BLE001 - a frame navigating away is still loading
            states.append("navigating")
    return tuple(states)


async def _progress(page: Any, requests: _Requests) -> tuple[Any, ...]:
    return (page.url, await _ready_states(page), requests.started, requests.settled)


def _settled(mark: tuple[Any, ...], requests: _Requests) -> bool:
    states = mark[1]
    return requests.started <= requests.settled and all(s == "complete" for s in states)


def _missing(page: Any, target: Any, locator: Any) -> BrowserError:
    steps = target.get("steps") if isinstance(target, dict) else None
    first = steps[0] if isinstance(steps, list) and steps and isinstance(steps[0], dict) else {}
    message = (
        f"no element on {page.url} matches {describe(target) or 'the target'} "
        f"(Playwright locator {locator}); the page finished loading and made no request "
        f"for {QUIET_S:g} s"
    )
    if first.get("by") == "ref":
        message += "; a ref names an element of the page's latest snapshot, so take a new one"
    return BrowserError(REASON_MISSING, message, url=page.url, target=describe(target))


async def resolve(page: Any, target: Any) -> Any:
    """The locator ``target`` describes on ``page``, once it matches at least one element.

    Raises:
        BrowserError: ``browser_target_invalid`` for a malformed target, or
            ``browser_target_missing`` when the settled page matches nothing.
    """
    locator = locator_for(page, target)
    if await locator.count():
        return locator
    with _Requests(page) as requests:
        mark = await _progress(page, requests)
        quiet_since = time.monotonic()
        while True:
            await asyncio.sleep(_POLL_S)
            if await locator.count():
                return locator
            now = await _progress(page, requests)
            if now != mark or not _settled(now, requests):
                mark, quiet_since = now, time.monotonic()
            elif time.monotonic() - quiet_since >= QUIET_S:
                raise _missing(page, target, locator)
