"""Certificates Chromium does not trust are the person's decision (browser addendum B).

Devices on a home or lab network (OctoPrint, Home Assistant, a NAS, a Jupyter server)
usually serve a self-signed certificate. The agent never bypasses a certificate error,
and no tool argument, setting or fragment lets it. A navigation that fails on one raises
``browser_certificate_untrusted`` with the host, Chromium's error name, the issuer and
subject, and the certificate's SHA-256 SPKI fingerprint; the page stays on Chromium's own
warning, so the person sees it in the shared tab, and the panel offers "Trust this
certificate" for the conversation or the workspace.

Trusting writes a ``browser_certificate`` policy row naming the host and the fingerprint:
the grant is pinned to the key, so a device that presents another certificate later is
asked about again. Chromium is launched with ``--ignore-certificate-errors-spki-list``
holding the granted fingerprints (Phase 0: it trusts exactly those keys and refuses a
different key on the same host; the per-request ``Security`` override works too but its
event carries no certificate, so it cannot check the key). A launch flag applies to the
whole browser, so a new grant relaunches it (:mod:`relaunch`), and the scope a grant names
is enforced before each agent call by :func:`check_call`: a page whose key was trusted for
another conversation, or one the person clicked through with Chromium's own Proceed (not a
grant: it lasts until the browser closes), is refused to the agent.

What a page presented is Chromium's own view, from ``Security.visibleSecurityStateChanged``
(the chain, subject, issuer and network error). In the ``cdp`` backend the warning is the
person's Chrome's business: the agent is told the same, the person proceeds there, and
CLIO writes no grant.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from clio_agent.gact.browser import settings
from clio_agent.gact.browser.settings import BrowserError
from clio_agent.runtime import trace

if TYPE_CHECKING:
    from clio_agent.gact.browser.service import BrowserService, PageRecord

__all__ = [
    "KIND",
    "attach",
    "check_call",
    "granted",
    "launch_args",
    "pinned",
    "spki_fingerprint",
    "state",
    "trust",
    "untrusted_error",
    "wait_seen",
]

KIND = "browser_certificate"
#: The page Chromium shows in place of one it would not load.
_ERROR_PAGE = "chrome-error://"
#: The visible security state of a page loaded although its certificate failed (Proceed).
_BROKEN = "insecure-broken"


# ---- the key ----------------------------------------------------------------------------
def _tlv(data: bytes, at: int) -> tuple[int, int, int]:
    """One DER element at ``at``: ``(tag, value start, end)``."""
    tag, length, start = data[at], data[at + 1], at + 2
    if length & 0x80:
        size = length & 0x7F
        length, start = int.from_bytes(data[start : start + size], "big"), start + size
    return tag, start, start + length


def spki_fingerprint(der: bytes) -> str:
    """The base64 SHA-256 of a certificate's SubjectPublicKeyInfo (Chromium's SPKI form)."""
    _tag, certificate, _end = _tlv(der, 0)
    _tag, field, _end = _tlv(der, certificate)  # tbsCertificate's first field
    tag, _start, end = _tlv(der, field)
    if tag == 0xA0:  # [0] version
        field = end
    for _ in range(5):  # serialNumber, signature, issuer, validity, subject
        _tag, _start, field = _tlv(der, field)
    tag, _start, end = _tlv(der, field)
    if tag != 0x30:
        raise ValueError("no SubjectPublicKeyInfo where the certificate should have one")
    return base64.b64encode(hashlib.sha256(der[field:end]).digest()).decode()


# ---- what the page presented ----------------------------------------------------------------
async def attach(record: "PageRecord", cdp: Any, main_frame: str) -> None:
    """Follow the page's main-frame requests, their failures and its security state."""
    documents: dict[str, str] = {}

    def requested(event: dict[str, Any]) -> None:
        if event.get("type") == "Document" and event.get("frameId") == main_frame:
            documents[event["requestId"]] = record.main_url = str(event["request"]["url"])
            record.load_error = None

    def failed(event: dict[str, Any]) -> None:
        url = documents.pop(event.get("requestId", ""), "")
        if url:
            record.load_error = {"url": url, "error": str(event.get("errorText") or "")}

    def secured(event: dict[str, Any]) -> None:
        record.certificate = _seen(event.get("visibleSecurityState") or {}, record.main_url)

    cdp.on("Network.requestWillBeSent", requested)
    cdp.on("Network.loadingFailed", failed)
    cdp.on("Security.visibleSecurityStateChanged", secured)
    await cdp.send("Network.enable")
    await cdp.send("Security.enable")


def _seen(visible: dict[str, Any], url: str) -> dict[str, Any] | None:
    held = visible.get("certificateSecurityState") or {}
    chain = held.get("certificate") or []
    if not chain:
        return None
    try:
        fingerprint = spki_fingerprint(base64.b64decode(chain[0]))
    except (ValueError, IndexError) as exc:  # a certificate the walker cannot read
        trace.event("BROWSER", "browser certificate unreadable url=%s error=%s", url[:200], exc)
        fingerprint = ""
    valid_to = held.get("validTo")
    return {
        "host": (urlsplit(url).hostname or "").lower(),
        "url": url,
        "state": str(visible.get("securityState") or ""),
        "error": str(held.get("certificateNetworkError") or "").removeprefix("net::"),
        "subject": str(held.get("subjectName") or ""),
        "issuer": str(held.get("issuer") or ""),
        "fingerprint": fingerprint,
        "valid_to": (
            datetime.fromtimestamp(float(valid_to), timezone.utc).isoformat()
            if isinstance(valid_to, (int, float))
            else ""
        ),
    }


async def wait_seen(record: "PageRecord", *, timeout_s: float = 1.5) -> dict[str, Any] | None:
    """The certificate a failed load presented; its security state can trail the failure."""
    deadline = asyncio.get_running_loop().time() + timeout_s
    while True:
        seen = record.certificate
        if seen and seen.get("error") and seen.get("url") == record.main_url:
            return seen
        if asyncio.get_running_loop().time() >= deadline:
            return seen if seen and seen.get("error") else None
        await asyncio.sleep(0.05)


def _showing_untrusted(record: "PageRecord") -> dict[str, Any] | None:
    """The certificate the page shows a warning for, or loaded through Proceed."""
    seen = record.certificate
    if not seen or not seen.get("error"):
        return None
    if record.url().startswith(_ERROR_PAGE) or seen.get("state") == _BROKEN:
        return seen
    return None


# ---- grants ---------------------------------------------------------------------------------
def pinned(app: Any) -> list[str]:
    """Every fingerprint the person trusted, for any conversation: the launch flag's list."""
    rows = getattr(getattr(app, "state", None), "permission_policies", None) or []
    keys = {
        str(row.get("fingerprint") or "")
        for row in rows
        if isinstance(row, dict)
        and row.get("kind") == KIND
        and str(row.get("action") or "").lower() != "deny"
    }
    return sorted(key for key in keys if key)


def launch_args(app: Any) -> list[str]:
    """Chromium's arguments for the granted keys; none in the ``cdp`` backend."""
    keys = pinned(app)
    return [f"--ignore-certificate-errors-spki-list={','.join(keys)}"] if keys else []


def granted(app: Any, session_id: str, host: str, fingerprint: str) -> bool:
    """Whether this conversation may use ``host`` while it presents this key."""
    if app is None or not fingerprint:
        return False
    from clio_agent.gact.runtime.grant_resolver import resolve  # noqa: PLC0415
    from clio_agent.gact.runtime.permission_actions import POLICY_ALLOWING  # noqa: PLC0415

    session = app.state.sessions.get(session_id)
    action = resolve(
        KIND,
        host,
        path=fingerprint,
        policies=getattr(app.state, "permission_policies", None),
        session_id=session_id,
        workspace_id=str(getattr(session, "workspace_id", "") or ""),
    )
    return action in POLICY_ALLOWING


def untrusted_error(
    record: "PageRecord", seen: dict[str, Any] | None, error: str = ""
) -> BrowserError:
    """``browser_certificate_untrusted`` for the page's current or last failed load."""
    seen = seen or {}
    host = str(seen.get("host") or (urlsplit(record.main_url).hostname or "")).lower()
    name = str(seen.get("error") or error or "ERR_CERT_INVALID")
    where = (
        "Ask the person to look at the page in their own browser and proceed there if they "
        "trust it."
        if settings.backend() == "cdp"
        else "Ask the person to look at the page in the shared tab: only they can trust this "
        "certificate, with Trust this certificate beside the warning."
    )
    issuer = str(seen.get("issuer") or "")
    fingerprint = str(seen.get("fingerprint") or "")
    about = "; ".join(
        part
        for part in (
            name,
            f"issued by {issuer}" if issuer else "",
            f"key sha256/{fingerprint}" if fingerprint else "",
        )
        if part
    )
    return BrowserError(
        "browser_certificate_untrusted",
        f"{host} presented a certificate the browser does not trust ({about}). {where} "
        "Do not try to get around the warning.",
        host=host,
        error=name,
        issuer=str(seen.get("issuer") or ""),
        subject=str(seen.get("subject") or ""),
        fingerprint=str(seen.get("fingerprint") or ""),
        page=record.id,
    )


def check_call(service: "BrowserService", record: "PageRecord", *, navigates: bool) -> None:
    """Before an agent call reads or acts on the page: its certificate is trusted here.

    A call that only navigates away is let through: it leaves the page in question.
    """
    if navigates or urlsplit(record.url()).scheme.lower() != "https":
        return
    seen = record.certificate
    if not seen or not seen.get("fingerprint"):
        return
    app, sid = service.app, record.session_id
    if seen.get("error"):  # loaded through the warning: the person's Proceed is not a grant
        if settings.backend() == "cdp" or granted(app, sid, record.host(), seen["fingerprint"]):
            return  # in the person's own Chrome, proceeding there is their decision (BR59)
        raise untrusted_error(record, seen)
    if seen["fingerprint"] in pinned(app) and not granted(
        app, sid, record.host(), seen["fingerprint"]
    ):  # trusted for another conversation or workspace, not this one
        raise untrusted_error(record, seen)


def state(record: "PageRecord") -> dict[str, Any]:
    """The warning the panel shows a Trust control for, when the page is on one."""
    seen = _showing_untrusted(record)
    if seen is None:
        return {}
    return {
        "certificate_untrusted": {
            **{key: seen.get(key, "") for key in ("host", "url", "error", "issuer", "subject")},
            "fingerprint": seen.get("fingerprint", ""),
            "valid_to": seen.get("valid_to", ""),
            "grantable": settings.backend() != "cdp" and bool(seen.get("fingerprint")),
        }
    }


def trust(service: "BrowserService", record: "PageRecord", scope: str) -> dict[str, Any]:
    """The person trusts the certificate the page warns about, then the browser relaunches.

    Raises:
        BrowserError: ``browser_certificate_none`` when the page shows no such warning,
            ``browser_certificate_cdp`` in the ``cdp`` backend (the person's own Chrome).
    """
    seen = _showing_untrusted(record)
    if seen is None or not seen.get("fingerprint"):
        raise BrowserError("browser_certificate_none", "this page shows no certificate warning")
    if settings.backend() == "cdp":
        raise BrowserError(
            "browser_certificate_cdp",
            "this page is in your own browser; proceed there if you trust the certificate",
        )
    app = service.app
    from clio_agent.gact.runtime.permission_policies import (  # noqa: PLC0415
        _appended,
        _flush_permission_policies,
    )

    session = app.state.sessions.get(record.session_id)
    scope_id = (
        record.session_id if scope == "session" else str(getattr(session, "workspace_id", ""))
    )
    _appended(
        app,
        {
            "kind": KIND,
            "scope": scope,
            "scope_id": scope_id,
            "host_pattern": seen["host"],
            "fingerprint": seen["fingerprint"],
            "action": "allow",
            "grantor": "user",
        },
    )
    _flush_permission_policies(app)
    trace.event(
        "BROWSER",
        "certificate trusted host=%s fingerprint=%s scope=%s:%s",
        seen["host"],
        seen["fingerprint"],
        scope,
        scope_id,
    )
    from clio_agent.gact.browser.relaunch import relaunch  # noqa: PLC0415

    service.submit(relaunch(service, record.profile))
    return {"host": seen["host"], "fingerprint": seen["fingerprint"], "scope": scope}
