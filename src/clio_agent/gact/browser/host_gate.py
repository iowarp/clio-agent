"""Which hosts a page may load: granted by the person, asked once per new host.

Every main-frame navigation of a CLIO page is decided here, in one place. A DevTools
``Fetch`` interception on each page pauses its main frame's ``Document`` requests at request
stage, which covers a link the agent clicks, a redirect hop, a form post and a script's own
navigation alike (BR20: Playwright's route handler misses redirect hops). Subframes and
subresources are not gated: they belong to the page the person or the agent chose.

A host is allowed when ``browser.granted_hosts`` or a ``browser_host`` policy row covers it.
The gate governs the agent, not the person: the shared tab is the person's browser too.

1. A navigation during an agent call on that page (a link it clicks, a redirect, a form)
   to a host not allowed asks the person, through the same interactive gate as any tool
   call, with the script the call came from; the call's clock and the script's clock
   pause while the question is open (BR17), and Stop cancels both.
2. A navigation outside any agent call loads: the person's link, URL bar, sign-in chain
   across providers, or the page's own redirect. One the person made (input or URL bar
   within ``browser.person_window_s``) grants its host for the conversation.
3. Before an agent call acts on a page, the page's current host must be allowed; a page
   the person or the page itself took to a new host asks first (:func:`admit_call`).

A denied hop fails with ``Aborted``, which keeps the page on the page it was on (Phase 0).
Only ``http`` and ``https`` navigations reach a host check; ``about:``, ``data:`` and
``blob:`` load, and every other scheme (``file:``, ``chrome:``) is refused, so a page can
never read the computer's files past CLIO's file policy.
"""

from __future__ import annotations

import asyncio
import fnmatch
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from clio_agent.gact.browser import settings
from clio_agent.gact.browser.settings import BrowserError
from clio_agent.runtime import trace

if TYPE_CHECKING:
    from clio_agent.gact.browser.service import BrowserService, PageRecord

__all__ = [
    "GRANTOR_PERSON_NAVIGATION",
    "admit_call",
    "allow_blocked",
    "attach",
    "decision_for",
    "grant",
]

#: How a host the person opened in the shared tab is recorded.
GRANTOR_PERSON_NAVIGATION = "user_navigation"
_LOCAL_SCHEMES = frozenset({"about", "data", "blob"})
_WEB_SCHEMES = frozenset({"http", "https"})
#: The open question per (conversation, host): pages loading the same new host at once
#: share one question and its answer (W8 asked twice). Touched only on the browser loop.
_questions: dict[tuple[str, str], "asyncio.Future[bool]"] = {}


def decision_for(app: Any, session_id: str, host: str) -> str:
    """``allow``, ``deny`` or ``""`` (ask) for ``host`` in this session."""
    host = host.strip().lower()
    if any(fnmatch.fnmatchcase(host, pattern) for pattern in settings.granted_hosts()):
        return "allow"
    if app is None:
        return ""
    from clio_agent.gact.runtime.grant_resolver import KIND_BROWSER_HOST, resolve  # noqa: PLC0415
    from clio_agent.gact.runtime.permission_actions import POLICY_ALLOWING  # noqa: PLC0415

    session = app.state.sessions.get(session_id)
    action = resolve(
        KIND_BROWSER_HOST,
        host,
        policies=getattr(app.state, "permission_policies", None),
        session_id=session_id,
        workspace_id=str(getattr(session, "workspace_id", "") or ""),
    )
    if action in POLICY_ALLOWING:
        return "allow"
    return "deny" if action == "deny" else ""


def grant(app: Any, session_id: str, host: str, *, grantor: str) -> None:
    """Grant ``host`` to this conversation (the person opened it, or allowed a blocked one)."""
    from clio_agent.gact.runtime.grant_resolver import KIND_BROWSER_HOST  # noqa: PLC0415
    from clio_agent.gact.runtime.permission_policies import (  # noqa: PLC0415
        _appended,
        _flush_permission_policies,
    )

    _appended(
        app,
        {
            "kind": KIND_BROWSER_HOST,
            "scope": "session",
            "scope_id": session_id,
            "host_pattern": host,
            "action": "allow",
            "grantor": grantor,
        },
    )
    _flush_permission_policies(app)
    trace.event("BROWSER", "host granted host=%s session=%s by=%s", host, session_id, grantor)


def _ask(app: Any, record: "PageRecord", host: str, url: str) -> bool:
    """Ask the person whether this page may load ``host``; runs in the agent call's context."""
    from clio_agent.gact import permission_timeout  # noqa: PLC0415
    from clio_agent.gact.code_mode import grants as script_grants  # noqa: PLC0415
    from clio_agent.gact.permission_delivery import publish_permission_event  # noqa: PLC0415
    from clio_agent.gact.permission_evidence import permission_identity  # noqa: PLC0415
    from clio_agent.gact.runtime.permission_actions import (  # noqa: PLC0415
        STANDARD_ANSWERS,
        allows,
    )
    from clio_agent.gact.runtime.permission_policies import (  # noqa: PLC0415
        BROWSER_HOST_REQUEST_KIND,
    )
    from clio_agent.gact.runtime.retention import enforce_dict_bound  # noqa: PLC0415

    sid = record.session_id
    pid = f"perm_{uuid.uuid4().hex[:12]}"
    event = threading.Event()
    row: dict[str, Any] = {
        "id": pid,
        "session_id": sid,
        **permission_identity(app, sid),
        "kind": BROWSER_HOST_REQUEST_KIND,
        "tool_call": {
            "tool_name": BROWSER_HOST_REQUEST_KIND,
            "input": {"host": host, "url": url, "page": record.id, "label": record.label},
        },
        "summary": f"Let the browser load {host}",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "status": "pending",
    }
    with script_grants.question():
        fields = script_grants.row_fields(list(STANDARD_ANSWERS))
        if fields:  # the script is shown; "rest of this script" does not grant a host
            fields["actions"] = [a for a in fields["actions"] if a != script_grants.ALLOW_SCRIPT]
            row.update(fields)
        app.state.permission_events[pid] = event
        app.state.permissions[pid] = row
        enforce_dict_bound(app, app.state.permissions, "permissions", session_id=sid)
        publish_permission_event(app, "permission.requested", owner_session_id=sid, payload=row)
        refused = permission_timeout.wait_for_permission(
            app,
            pid,
            row,
            event,
            tool_name=BROWSER_HOST_REQUEST_KIND,
            cancelled=app.state.cancel_events.get(sid),
        )
    return refused is None and allows(str(row.get("action") or "deny"))


async def _decide(service: "BrowserService", record: "PageRecord", url: str) -> bool:
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    if scheme in _LOCAL_SCHEMES:
        return True
    if scheme not in _WEB_SCHEMES:
        trace.event("BROWSER", "browser_scheme_refused url=%s page=%s", url[:200], record.id)
        _blocked(service, record, scheme + ":", url, reason="browser_scheme_refused")
        return False
    host = (parts.hostname or "").lower()
    inflight = record.inflight
    if inflight is not None:
        inflight.navigated = True
    decision = decision_for(service.app, record.session_id, host)
    if decision == "allow":
        return True
    if inflight is None:  # the person's browsing, or the page's own: it loads
        person = time.monotonic() - record.last_person_input < settings.person_window_s()
        if person and decision != "deny":
            await asyncio.get_running_loop().run_in_executor(
                None,
                lambda: grant(
                    service.app, record.session_id, host, grantor=GRANTOR_PERSON_NAVIGATION
                ),
            )
        return True
    if decision == "deny":
        inflight.denied_host = host
        return False
    allowed = await _ask_for_call(service, record, host, url)
    if not allowed:
        inflight.denied_host = host
    return allowed


async def admit_call(service: "BrowserService", record: "PageRecord") -> None:
    """Before an agent call acts on the page: its host must be one the agent may use.

    The person, or the page, may have taken the page to a host the agent was never
    allowed; the agent asks before reading or acting on it, as for a navigation.
    """
    parts = urlsplit(record.url())
    if parts.scheme.lower() not in _WEB_SCHEMES:
        return
    host = (parts.hostname or "").lower()
    decision = decision_for(service.app, record.session_id, host)
    if decision == "allow":
        return
    if decision != "deny" and await _ask_for_call(service, record, host, record.url()):
        return
    raise BrowserError(
        "browser_host_denied",
        f"the person did not allow the agent to use {host}; the page is on it",
        host=host,
        page=record.id,
    )


async def _ask_for_call(
    service: "BrowserService", record: "PageRecord", host: str, url: str
) -> bool:
    inflight = record.inflight
    assert inflight is not None
    inflight.waiting = True
    started = time.monotonic()
    key = (record.session_id, host)
    try:
        pending = _questions.get(key)
        if pending is not None:  # another page of this conversation is asking already
            return await asyncio.shield(pending)
        context = inflight.context.copy()
        question = asyncio.get_running_loop().run_in_executor(
            None, lambda: context.run(_ask, service.app, record, host, url)
        )
        _questions[key] = question
        try:
            return await asyncio.shield(question)
        finally:
            if _questions.get(key) is question:
                del _questions[key]
    finally:
        inflight.paused_s += time.monotonic() - started
        inflight.waiting = False


def _blocked(
    service: "BrowserService",
    record: "PageRecord",
    host: str,
    url: str,
    *,
    reason: str,
    detail: str = "",
) -> None:
    from clio_agent.gact.browser.service import PAGE_UPDATED  # noqa: PLC0415

    record.navigation_blocked = {
        "host": host,
        "url": url,
        "reason": reason,
        "at": datetime.now(timezone.utc).isoformat(),
        **({"detail": detail[:300]} if detail else {}),
    }
    record.blocked_reported = False
    trace.event("BROWSER", "%s host=%s page=%s", reason, host, record.id)
    service.publish(PAGE_UPDATED, record)


async def attach(service: "BrowserService", record: "PageRecord") -> None:
    """Gate every main-frame navigation of ``record``'s page."""
    page = record.page
    cdp = await page.context.new_cdp_session(page)
    tree = await cdp.send("Page.getFrameTree")
    main_frame = tree["frameTree"]["frame"]["id"]

    async def paused(event: dict[str, Any]) -> None:
        request_id = event["requestId"]
        allowed = True
        try:
            if event.get("frameId") == main_frame:
                record.loading = True
                allowed = await _decide(service, record, str(event["request"]["url"]))
        except Exception as exc:  # noqa: BLE001 - forwards: refused, as a typed block
            allowed = False  # a gate failure refuses, never allows
            url = str(event.get("request", {}).get("url") or "")
            _blocked(service, record, "", url, reason="browser_host_gate_error", detail=str(exc))
        try:
            if allowed:
                await cdp.send("Fetch.continueRequest", {"requestId": request_id})
            else:
                record.loading = False
                await cdp.send(
                    "Fetch.failRequest", {"requestId": request_id, "errorReason": "Aborted"}
                )
        except settings.page_errors() as exc:  # the page closed meanwhile
            trace.event("BROWSER", "browser gate reply skipped page=%s error=%s", record.id, exc)

    cdp.on("Fetch.requestPaused", lambda event: asyncio.ensure_future(paused(event)))
    await cdp.send(
        "Fetch.enable", {"patterns": [{"resourceType": "Document", "requestStage": "Request"}]}
    )


def allow_blocked(service: "BrowserService", record: "PageRecord") -> str:
    """The person allows the host a page was blocked from, and the page loads it."""
    blocked = record.navigation_blocked
    if not blocked or blocked.get("reason") == "browser_scheme_refused":
        return ""
    host, url = str(blocked["host"]), str(blocked["url"])
    grant(service.app, record.session_id, host, grantor="user")
    record.navigation_blocked = None
    record.last_person_input = time.monotonic()
    service.submit(record.page.goto(url))
    return host
