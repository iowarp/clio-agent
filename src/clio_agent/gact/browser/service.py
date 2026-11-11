"""The browser service: one Chromium per profile, pages owned by sessions, one event loop.

Playwright's async API runs on one event loop thread owned by this module, started by the
first browser call. A tool call arrives on a dispatch worker thread and submits its work to
that loop with :func:`asyncio.run_coroutine_threadsafe`, so a browser call never runs on the
application's event loop. The calling thread waits by polling: Stop (the turn's cooperative
cancellation) cancels the work, and ``browser.action_timeout_s`` bounds it, net of the time
spent waiting for the person's answer to a host question (BR17; Playwright's own timeouts
are off, since its clock cannot pause).

A profile is one persistent Chromium context (``<state>/browser/profiles/<name>``), so
sign-ins survive restarts and every session shares them (BR5). A page belongs to the session
that opened it and outlives that session's ``run_code`` kernel: a cancelled script or a
restarted kernel never loses a tab. Pages close when their session is deleted or, for a
child session, when its task settles (:func:`close_session`). A profile with no pages for
``browser.idle_minutes`` closes its browser.

Chromium runs with its own sandbox, never inside CLIO's write fence (plan, "Chromium's own
sandbox"). When the sandbox cannot start, the launch is refused with
``browser_sandbox_unavailable`` unless ``browser.allow_unsandboxed`` is set.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextvars
import itertools
import os
import tempfile
import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from clio_agent import paths
from clio_agent.gact.browser import certificates, failures, favicon, install, picker, settings
from clio_agent.gact.browser.driver_loop import driver_loop
from clio_agent.gact.browser.settings import BrowserError, errors, page_errors
from clio_agent.runtime import trace

__all__ = [
    "Inflight",
    "PageRecord",
    "Profile",
    "close_all",
    "close_session",
    "service",
    "switch_browser",
]

PAGE_OPENED = "browser.page.opened"
PAGE_UPDATED = "browser.page.updated"
PAGE_CLOSED = "browser.page.closed"
_SANDBOX_MARKERS = ("sandboxing failed", "no usable sandbox", "without --no-sandbox")
_POLL_S = 0.1


@dataclass
class Inflight:
    """The agent call running on a page now: what a held navigation is attributed to."""

    action: str
    context: contextvars.Context
    started: float = field(default_factory=time.monotonic)
    #: Seconds spent waiting for the person (not counted against the action clock).
    paused_s: float = 0.0
    waiting: bool = False
    #: A main-frame navigation was requested during this call.
    navigated: bool = False
    #: The host the person denied during this call, if any.
    denied_host: str = ""
    #: The call only navigates (goto, back, open): it may leave a page the agent may not use.
    navigates: bool = False


@dataclass
class PageRecord:
    """One page: its owner, its state for the agent's next result and for the live view."""

    id: str
    session_id: str
    label: str
    profile: str
    page: Any
    lock: asyncio.Lock
    opened_at: float = field(default_factory=time.time)
    inflight: Inflight | None = None
    person_acted: bool = False
    last_person_input: float = float("-inf")  # never, until the person acts
    #: The host the page was last refused; shown by the panel until the page loads again.
    navigation_blocked: dict[str, Any] | None = None
    #: Whether the agent's results already reported that refusal (it is told once).
    blocked_reported: bool = False
    downloads: list[dict[str, Any]] = field(default_factory=list)
    saving: set[asyncio.Task[Any]] = field(default_factory=set)
    #: Main-frame responses that were attachments: downloads the call should wait for.
    downloads_expected: int = 0
    downloads_started: int = 0
    last_action: str = ""
    #: When the agent, the person or a viewer last used the page (idle pages close).
    last_used: float = field(default_factory=time.monotonic)
    #: Live-view windows watching the page now; a watched page never counts as idle.
    viewers: int = 0
    title: str = ""
    #: The page's icon as a ``data:`` URL, and the address it was read from (:mod:`favicon`).
    favicon: str = ""
    favicon_src: str = ""
    loading: bool = False
    closed: bool = False
    #: The last main-frame address requested, and why it failed if it did.
    main_url: str = ""
    load_error: dict[str, Any] | None = None
    #: What the page's certificate was, as Chromium saw it (browser addendum B).
    certificate: dict[str, Any] | None = None
    #: The browser is relaunching under this page (it keeps its id), or just did.
    relaunching: bool = False
    restarted: bool = False

    def host(self) -> str:
        return (urlsplit(self.url()).hostname or "").lower()

    def url(self) -> str:
        try:
            return str(self.page.url)
        except page_errors():  # a closed page has no URL
            return ""


@dataclass
class Profile:
    """One profile's browser: a persistent context (managed) or a connected Chrome (cdp)."""

    name: str
    context: Any
    browser: Any = None
    sandboxed: bool = True
    sandbox_reason: str = ""
    egress_channel: str = ""
    last_used: float = field(default_factory=time.monotonic)


def _state_root() -> Path:
    return paths.user_state_dir() / "browser"


#: Chromium keeps its process-singleton socket in ``TMPDIR``; a Unix socket path must stay
#: under about 100 bytes, so a long ``TMPDIR`` aborts the launch ("Socket path too long").
_SOCKET_PATH_BUDGET = 60


def _chromium_env() -> dict[str, str]:
    """The environment Chromium runs with: the server's, with a short ``TMPDIR`` it owns."""
    env = dict(os.environ)
    base = Path(tempfile.gettempdir())
    if len(str(base)) > _SOCKET_PATH_BUDGET and os.name == "posix":
        base = Path("/tmp")
    folder = base / f"clio-browser-{os.getuid() if hasattr(os, 'getuid') else 'user'}"
    folder.mkdir(mode=0o700, exist_ok=True)
    env["TMPDIR"] = str(folder)
    return env


class BrowserService:
    """The one owner of the agent's browser (plan rule 3)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._playwright: Any = None
        self.profiles: dict[str, Profile] = {}
        self.pages: dict[str, PageRecord] = {}
        self._ids = itertools.count(1)
        self._reaper: threading.Thread | None = None
        self.app: Any = None
        #: Called on every page change, e.g. the live view's state push.
        self.listeners: list[Callable[[str, PageRecord], None]] = []
        #: Awaited when an agent call on a page starts (True) and ends (False), on the loop;
        #: the live view shows the agent's action annotations only meanwhile.
        self.agent_hooks: list[Callable[[PageRecord, bool], Awaitable[None]]] = []

    # ---- the loop ----------------------------------------------------------------------
    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        with self._lock:
            if self._loop is not None and self._thread is not None and self._thread.is_alive():
                return self._loop
            loop = driver_loop()
            ready = threading.Event()

            def run() -> None:
                asyncio.set_event_loop(loop)
                loop.call_soon(ready.set)
                loop.run_forever()

            self._thread = threading.Thread(target=run, name="clio-browser-loop", daemon=True)
            self._thread.start()
            ready.wait(10)
            self._loop = loop
            self._start_reaper()
            return loop

    def submit(self, coro: Awaitable[Any]) -> concurrent.futures.Future[Any]:
        """Run ``coro`` on the browser loop; the caller waits on the returned future."""
        return asyncio.run_coroutine_threadsafe(coro, self._ensure_loop())  # type: ignore[arg-type]

    def wait(
        self,
        coro: Awaitable[Any],
        *,
        inflight: Inflight | None = None,
        limit_s: float | None = None,
    ) -> Any:
        """Run ``coro`` on the loop and wait, honouring Stop and the action clock.

        ``limit_s`` lengthens the clock for a call that asked to wait longer (a
        ``browser_wait`` with its own ``timeout_s``); it never shortens it.
        """
        future = self.submit(coro)
        from clio_agent.runtime.cancellation import cancellation_requested  # noqa: PLC0415

        limit = max(settings.action_timeout_s(), float(limit_s or 0) + 1.0)
        started = time.monotonic()
        while True:
            try:
                return future.result(timeout=_POLL_S)
            except concurrent.futures.TimeoutError:
                pass
            if cancellation_requested():
                future.cancel()
                raise BrowserError("browser_action_cancelled", "the turn was stopped")
            paused = inflight.paused_s if inflight is not None else 0.0
            waiting = inflight is not None and inflight.waiting
            if not waiting and time.monotonic() - started - paused > limit:
                future.cancel()
                raise BrowserError(
                    "browser_action_timeout",
                    f"the action ran past {limit:g} s (browser.action_timeout_s)",
                )

    # ---- profiles ----------------------------------------------------------------------
    async def _pw(self) -> Any:
        if self._playwright is None:
            from playwright.async_api import async_playwright  # noqa: PLC0415 - lazy (install)

            from clio_agent.gact.browser import driver  # noqa: PLC0415

            # A desktop bundle fetches Playwright's Node.js on first use; like Chromium's
            # download it outlives the action and answers browser_runtime_preparing.
            await asyncio.get_running_loop().run_in_executor(
                None, driver.driver_for_action, settings.action_timeout_s() / 2
            )
            self._playwright = await async_playwright().start()
        return self._playwright

    async def profile(self, session_id: str) -> Profile:
        name = settings.profile()
        found = self.profiles.get(name)
        if found is not None:
            found.last_used = time.monotonic()
            return found
        pw = await self._pw()
        if settings.backend() == "cdp":
            found = await self._connect(pw, name)
        else:
            found = await self._launch(pw, name, session_id)
            await picker.install(found.context)  # comments on pages (comments plan, FB17)
        found.context.on("page", lambda page: asyncio.ensure_future(self._adopt(page)))
        self.profiles[name] = found
        return found

    async def _connect(self, pw: Any, name: str) -> Profile:
        from clio_agent.gact.browser import chrome_discovery  # noqa: PLC0415

        url = settings.cdp_url()
        if not url:
            raise BrowserError("browser_cdp_url_missing", "browser.backend is cdp but no url")
        if url.lower() in chrome_discovery.PRODUCTS:  # the person's own running Chrome or Edge
            url = chrome_discovery.devtools_endpoint(url.lower())
        try:
            browser = await pw.chromium.connect_over_cdp(url, no_defaults=True)
        except Exception as exc:  # noqa: BLE001 - forwards: a typed browser_cdp_unreachable
            raise BrowserError(
                "browser_cdp_unreachable", f"no Chrome answers at {url}: {exc}"
            ) from exc
        context = browser.contexts[0] if browser.contexts else await browser.new_context()
        trace.event("BROWSER", "connected cdp url=%s", url)
        return Profile(name=name, context=context, browser=browser)

    async def _launch(self, pw: Any, name: str, session_id: str) -> Profile:
        loop = asyncio.get_running_loop()
        product = settings.channel()
        executable = (
            # Half the action's clock: Playwright's start shares it, and a longer
            # first-use download answers browser_runtime_preparing rather than timing out.
            await loop.run_in_executor(
                None, install.chromium_for_action, settings.action_timeout_s() / 2
            )
            if product == "chromium"
            else None
        )
        # Each browser keeps its own profile folder: their formats differ.
        user_data = (
            _state_root() / "profiles" / (name if product == "chromium" else f"{name}-{product}")
        )
        user_data.mkdir(parents=True, exist_ok=True)
        from clio_agent.gact.browser.egress import proxy_for  # noqa: PLC0415

        proxy, channel = proxy_for(self.app, session_id)
        options: dict[str, Any] = {
            **({"executable_path": str(executable)} if executable else {"channel": product}),
            "headless": True,
            "accept_downloads": True,
            "viewport": {"width": 1280, "height": 800},
            "env": _chromium_env(),
            # The certificates the person trusted, pinned to their keys (browser addendum B).
            "args": certificates.launch_args(self.app),
        }
        if proxy:
            options["proxy"] = proxy
        sandboxed, reason = True, ""
        try:
            context = await pw.chromium.launch_persistent_context(
                str(user_data), chromium_sandbox=True, **options
            )
        except Exception as exc:  # noqa: BLE001 - forwards: classified into a typed refusal
            text = str(exc)
            libraries = install.missing_libraries(text)
            if libraries:
                raise BrowserError(
                    install.REASON_MISSING_LIBRARIES,
                    "Chromium needs system libraries this computer lacks: " + ", ".join(libraries),
                    libraries=libraries,
                ) from exc
            if not any(marker in text.lower() for marker in _SANDBOX_MARKERS):
                raise BrowserError("browser_launch_failed", text[-1500:]) from exc
            reason = next(
                (line.strip() for line in text.splitlines() if "sandbox" in line.lower()),
                "Chromium's sandbox could not start",
            )
            if not settings.allow_unsandboxed():
                install.record_sandbox(sandboxed=False, reason=reason)
                raise BrowserError(
                    "browser_sandbox_unavailable",
                    f"{reason}. Run CLIO as a normal user, or set browser.allow_unsandboxed "
                    "to run Chromium without its sandbox.",
                ) from exc
            sandboxed = False
            trace.event("BROWSER", "browser_sandbox_off reason=%s", reason)
            context = await pw.chromium.launch_persistent_context(
                str(user_data), chromium_sandbox=False, **options
            )
        install.record_sandbox(sandboxed=sandboxed, reason=reason)
        context.set_default_timeout(0)
        context.set_default_navigation_timeout(0)
        for blank in list(context.pages):  # the launch opens one empty tab nobody owns
            await blank.close()
        trace.event("BROWSER", "launched profile=%s sandboxed=%s", name, sandboxed)
        return Profile(
            name=name,
            context=context,
            sandboxed=sandboxed,
            sandbox_reason=reason,
            egress_channel=channel,
        )

    # ---- pages -------------------------------------------------------------------------
    def _new_id(self) -> str:
        return f"p{next(self._ids)}"

    async def _register(
        self, session_id: str, label: str, page: Any, profile: Profile
    ) -> PageRecord:
        record = PageRecord(
            id=self._new_id(),
            session_id=session_id,
            label=label,
            profile=profile.name,
            page=page,
            lock=asyncio.Lock(),
        )
        self.pages[record.id] = record
        await self._bind(record, page)
        self.publish(PAGE_OPENED, record)
        return record

    async def _bind(self, record: PageRecord, page: Any) -> None:
        """Give ``record`` its browser page: listeners, the host gate, no default timeouts."""
        from clio_agent.gact.browser import host_gate  # noqa: PLC0415

        record.page = page
        page.set_default_timeout(0)
        page.set_default_navigation_timeout(0)
        page.on("download", lambda download: self._on_download(record, download))
        page.on("response", lambda response: self._on_response(record, response))
        page.on("close", lambda closed: self._forget(record) if closed is record.page else None)
        page.on("load", lambda _page: self._loaded(record))
        await host_gate.attach(self, record)

    async def open(self, session_id: str, label: str) -> PageRecord:
        open_pages = [p for p in self.pages.values() if not p.closed]
        if len(open_pages) >= settings.max_pages():
            mine = sum(1 for p in open_pages if p.session_id == session_id)
            others = len({p.session_id for p in open_pages if p.session_id != session_id})
            raise BrowserError(
                "browser_page_limit",
                f"{len(open_pages)} pages are open (browser.max_pages): {mine} in this "
                f"conversation and {len(open_pages) - mine} in {others} other conversations. "
                + (
                    "Close one of this conversation's pages first."
                    if mine
                    else "The others close after browser.idle_minutes unused, or the person "
                    "can close them in their Browser tabs."
                ),
                mine=mine,
                others=len(open_pages) - mine,
            )
        profile = await self.profile(session_id)
        page = await profile.context.new_page()
        return await self._register(session_id, label, page, profile)

    async def _adopt(self, page: Any) -> None:
        """A page the page itself opened (a popup) belongs to its opener's session."""
        if any(r.page is page for r in self.pages.values()):
            return
        try:
            opener = await page.opener()
        except page_errors():  # a page that closed already
            return
        parent = next((r for r in self.pages.values() if r.page is opener), None)
        if parent is None:
            return  # not CLIO's (a person's own tab in the cdp backend)
        profile = self.profiles.get(parent.profile)
        if profile is not None:
            await self._register(parent.session_id, f"{parent.label} (popup)", page, profile)

    def _forget(self, record: PageRecord) -> None:
        if record.closed or record.relaunching:
            return
        record.closed = True
        self.pages.pop(record.id, None)
        self.publish(PAGE_CLOSED, record)

    def _loaded(self, record: PageRecord) -> None:
        record.loading = False
        record.navigation_blocked = None  # a page that loaded is past any refusal
        asyncio.ensure_future(self._retitle(record))

    async def _retitle(self, record: PageRecord) -> None:
        try:
            record.title = await record.page.title()
        except page_errors():  # the page closed while loading
            record.title = ""
        self.publish(PAGE_UPDATED, record)
        known = record.favicon
        await favicon.refresh(record)
        if record.favicon != known:
            self.publish(PAGE_UPDATED, record)

    def owned(self, session_id: str, page_id: str) -> PageRecord:
        record = self.pages.get(page_id)
        if record is None or record.closed or record.session_id != session_id:
            raise BrowserError(
                "browser_page_not_found",
                f"this conversation has no open page {page_id!r}; browser.pages() lists them",
            )
        return record

    def session_pages(self, session_id: str) -> list[PageRecord]:
        return [r for r in self.pages.values() if r.session_id == session_id and not r.closed]

    # ---- agent calls -------------------------------------------------------------------
    def run(
        self,
        record: PageRecord,
        action: str,
        work: Callable[[PageRecord], Awaitable[Any]],
        *,
        limit_s: float | None = None,
        navigates: bool = False,
    ) -> Any:
        """Run one agent action on ``record`` with the page lock held and the clock running.

        The calling thread's context is captured, so a navigation this action causes is
        asked about inside it: the question shows the script and pauses its clock too.
        """
        inflight = Inflight(action=action, context=contextvars.copy_context(), navigates=navigates)
        return self.wait(self._locked(record, inflight, work), inflight=inflight, limit_s=limit_s)

    async def _locked(
        self,
        record: PageRecord,
        inflight: Inflight,
        work: Callable[[PageRecord], Awaitable[Any]],
    ) -> Any:
        async with record.lock:
            record.inflight = inflight
            record.last_used = time.monotonic()
            record.last_action = inflight.action
            self.publish(PAGE_UPDATED, record)
            await self._agent_hook(record, True)
            try:
                from clio_agent.gact.browser import host_gate  # noqa: PLC0415

                await host_gate.admit_call(self, record, navigates=inflight.navigates)
                result = await work(record)
                if inflight.denied_host:  # a click or form led to a host the person refused
                    raise failures.denied(inflight)
                if inflight.navigated:
                    await record.page.wait_for_load_state("load")
                await self._settle_downloads(record)
                return result
            except BrowserError:
                raise
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - forwards: Playwright's errors as typed reasons
                raise await failures.classify(self, record, inflight, exc) from exc
            finally:
                await self._agent_hook(record, False)
                record.inflight = None
                record.last_used = time.monotonic()
                self.publish(PAGE_UPDATED, record)

    async def _agent_hook(self, record: PageRecord, active: bool) -> None:
        for hook in list(self.agent_hooks):
            try:
                await hook(record, active)
            except page_errors() as exc:  # a closed viewer page never breaks the agent's call
                trace.event("BROWSER", "agent hook failed page=%s error=%s", record.id, exc)

    def take_notes(self, record: PageRecord) -> dict[str, Any]:
        """What the agent's result reports since its previous call on this page, then reset."""
        notes: dict[str, Any] = {"person_acted": record.person_acted}
        if record.navigation_blocked and not record.blocked_reported:
            notes["navigation_blocked"] = record.navigation_blocked
            record.blocked_reported = True
        if record.downloads:
            notes["downloads"] = list(record.downloads)
        if record.restarted:  # a trusted certificate relaunched the browser under the page
            notes["browser_restarted"] = True
            record.restarted = False
        record.person_acted = False
        record.downloads.clear()
        return notes

    # ---- downloads ---------------------------------------------------------------------
    def _on_response(self, record: PageRecord, response: Any) -> None:
        try:
            request = response.request
            if request.frame.parent_frame is not None or not request.is_navigation_request():
                return
            disposition = response.headers.get("content-disposition", "")
        except page_errors():  # a response of a page that closed
            return
        if disposition.lower().startswith("attachment"):
            record.downloads_expected += 1

    async def _settle_downloads(self, record: PageRecord) -> None:
        """Wait for the downloads this call started to be saved (bounded by the clock)."""
        while record.downloads_started < record.downloads_expected:
            await asyncio.sleep(0.05)
        if record.saving:
            await asyncio.gather(*record.saving, return_exceptions=True)

    def _on_download(self, record: PageRecord, download: Any) -> None:
        record.downloads_started += 1
        task = asyncio.ensure_future(self._save(record, download))
        record.saving.add(task)
        task.add_done_callback(record.saving.discard)

    async def _save(self, record: PageRecord, download: Any) -> None:
        from clio_agent.runtime.session_scratch import session_scratch_dir  # noqa: PLC0415

        scratch = session_scratch_dir(record.session_id)
        if scratch is None:
            record.downloads.append(
                {"name": download.suggested_filename, "error": "browser_scratch_unavailable"}
            )
            await download.cancel()
            return
        folder = scratch / "downloads"
        folder.mkdir(parents=True, exist_ok=True)
        target = _unique(folder / Path(download.suggested_filename or "download").name)
        try:
            await download.save_as(str(target))
        except Exception as exc:  # noqa: BLE001 - forwards: into the call's downloads
            record.downloads.append(
                {"name": target.name, "error": f"browser_download_failed: {exc}"}
            )
            return
        record.downloads.append(
            {
                "name": target.name,
                "path": str(target),
                "size_bytes": target.stat().st_size,
                "url": download.url,
            }
        )

    # ---- closing -----------------------------------------------------------------------
    async def _close_pages(self, records: list[PageRecord]) -> None:
        for record in records:
            try:
                await record.page.close()
            except page_errors() as exc:  # a page that closed itself already
                trace.event("BROWSER", "page close skipped id=%s error=%s", record.id, exc)
            self._forget(record)

    def close_page(self, record: PageRecord) -> None:
        self.wait(self._close_pages([record]))

    def close_session(self, session_id: str) -> int:
        """Close every page of a session (deleted, or a child whose task settled)."""
        records = self.session_pages(session_id)
        if records and self._loop is not None:
            self.submit(self._close_pages(records)).result(timeout=30)
        return len(records)

    async def _close_profile(self, profile: Profile) -> None:
        self.profiles.pop(profile.name, None)
        try:
            if profile.browser is not None:  # cdp: disconnect, never close the person's Chrome
                await profile.browser.close()
            else:
                await profile.context.close()
        finally:
            if profile.egress_channel:
                from clio_agent.gact.browser.egress import close_channel  # noqa: PLC0415

                close_channel(profile.egress_channel)
        trace.event("BROWSER", "profile closed name=%s", profile.name)

    async def _shutdown(self) -> None:
        await self._close_pages(list(self.pages.values()))
        for profile in list(self.profiles.values()):
            await self._close_profile(profile)
        if self._playwright is not None:
            await self._playwright.stop()
            self._playwright = None

    def close_all(self) -> None:
        """Close every page and browser and stop the loop (server shutdown)."""
        loop = self._loop
        if loop is None:
            return
        try:
            self.submit(self._shutdown()).result(timeout=30)
        except errors(TimeoutError, BrowserError) as exc:  # shutdown goes on
            trace.event("BROWSER", "browser shutdown incomplete error=%s", exc)
        loop.call_soon_threadsafe(loop.stop)
        with self._lock:
            self._loop = None

    def switch_browser(self) -> int:
        """Close every page and browser, keeping the loop, after Settings named another
        browser; the next page opens in that one. Returns how many pages closed."""
        if self._loop is None or not (self.pages or self.profiles):
            return 0
        closed = len(self.pages)
        trace.event("BROWSER", "browser switched pages_closed=%d", closed)
        self.submit(self._shutdown()).result(timeout=30)
        return closed

    def reap_idle(self) -> list[str]:
        """Close pages, then browsers, unused for ``browser.idle_minutes`` (BR32).

        A page is unused when no agent call, person input or live viewer touched it for
        that long; its conversation can open the URL again. A profile's browser closes
        once it has no pages for that long.
        """
        if self._loop is None:
            return []
        limit = settings.idle_minutes() * 60
        now = time.monotonic()
        stale = [
            r
            for r in list(self.pages.values())
            if not r.closed
            and r.inflight is None
            and r.viewers == 0
            and now - max(r.last_used, r.last_person_input) > limit
        ]
        if stale:
            trace.event("BROWSER", "browser_page_idle_closed pages=%s", [r.id for r in stale])
            self.submit(self._close_pages(stale)).result(timeout=30)
        idle = [
            p
            for p in self.profiles.values()
            if now - p.last_used > limit
            and not any(r.profile == p.name for r in self.pages.values())
        ]
        for profile in idle:
            self.submit(self._close_profile(profile)).result(timeout=30)
        return [p.name for p in idle]

    def _start_reaper(self) -> None:
        if self._reaper is not None and self._reaper.is_alive():
            return

        def reap() -> None:
            while self._loop is not None:
                time.sleep(60)
                try:
                    self.reap_idle()
                except errors(BrowserError, RuntimeError) as exc:  # the reaper keeps running
                    trace.event("BROWSER", "browser reap failed error=%s", exc)

        self._reaper = threading.Thread(target=reap, name="clio-browser-reaper", daemon=True)
        self._reaper.start()

    # ---- events and state --------------------------------------------------------------
    def state(self, record: PageRecord) -> dict[str, Any]:
        """The page as the UI and the live view see it (on Chromium's error page, the address
        it could not load)."""
        profile = self.profiles.get(record.profile)
        shown = record.main_url if record.url().startswith("chrome-error:") else record.url()
        return {
            "id": record.id,
            "session_id": record.session_id,
            "label": record.label,
            "url": shown,
            "host": (urlsplit(shown).hostname or "").lower(),
            "title": record.title,
            "favicon": record.favicon,
            "loading": record.loading,
            "agent_acting": record.inflight is not None,
            "last_action": record.last_action,
            "navigation_blocked": record.navigation_blocked,
            "sandbox_off": profile is not None and not profile.sandboxed,
            "sandbox_reason": profile.sandbox_reason if profile is not None else "",
            "closed": record.closed,
            **certificates.state(record),
        }

    def publish(self, kind: str, record: PageRecord) -> None:
        """Tell the session's readers that one of its pages changed."""
        for listener in list(self.listeners):
            try:
                listener(kind, record)
            except errors(RuntimeError) as exc:  # a closed reader never breaks the page
                trace.event("BROWSER", "page listener failed error=%s", exc)
        bus = getattr(getattr(self.app, "state", None), "bus", None)
        if bus is None:
            return
        from clio_agent.gact.events import Event  # noqa: PLC0415

        bus.publish(Event(type=kind, session_id=record.session_id, payload=self.state(record)))


def _unique(path: Path) -> Path:
    if not path.exists():
        return path
    for n in itertools.count(1):
        candidate = path.with_name(f"{path.stem} ({n}){path.suffix}")
        if not candidate.exists():
            return candidate
    raise AssertionError("unreachable")


_SERVICE: BrowserService | None = None
_SERVICE_LOCK = threading.Lock()


def service() -> BrowserService:
    """The server's browser service (created on first use)."""
    global _SERVICE
    with _SERVICE_LOCK:
        if _SERVICE is None:
            _SERVICE = BrowserService()
        return _SERVICE


def close_session(session_id: str) -> int:
    """Close a session's pages, if the service ever started; how many closed."""
    current = _SERVICE
    return current.close_session(session_id) if current is not None else 0


def close_all() -> None:
    """Close every browser (server shutdown), if the service ever started."""
    current = _SERVICE
    if current is not None:
        current.close_all()


def switch_browser() -> int:
    """Apply a changed browser setting at once, if the service ever started."""
    current = _SERVICE
    return current.switch_browser() if current is not None else 0
