"""A page's icon, so its workbench tab can be told apart from the others.

The icon is read through the page's own browser context, as the page itself loads it: the
same profile, cookies and proxy, never the person's own browser. Subresources are not
host-gated (:mod:`host_gate`), and the icon is one. The UI receives it as a small ``data:``
URL in the page state, so it needs no request of its own. A page with no usable icon has
none, and its tab keeps the generic browser icon.
"""

from __future__ import annotations

import base64
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from clio_agent.gact.browser.settings import errors
from clio_agent.runtime import trace

if TYPE_CHECKING:
    from clio_agent.gact.browser.service import PageRecord

__all__ = ["refresh"]

#: Larger icons are skipped: every page event carries the icon.
MAX_BYTES = 32 * 1024
_TIMEOUT_MS = 5_000
_WEB = frozenset({"http", "https"})

#: The page's declared icon (a tab-sized ``icon`` before an ``apple-touch-icon``), or its
#: origin's ``/favicon.ico``.
_FIND_JS = """() => {
  const links = [...document.querySelectorAll('link[rel][href]')];
  const rels = (link) => link.rel.toLowerCase().split(/\\s+/);
  const icon =
    links.find((link) => rels(link).includes('icon')) ??
    links.find((link) => rels(link).some((rel) => rel.startsWith('apple-touch-icon')));
  if (icon) return icon.href;
  return location.protocol.startsWith('http') ? `${location.origin}/favicon.ico` : '';
}"""

_SIGNATURES = (
    (b"\x89PNG", "image/png"),
    (b"\x00\x00\x01\x00", "image/x-icon"),
    (b"GIF8", "image/gif"),
    (b"\xff\xd8\xff", "image/jpeg"),
)


def _media_type(body: bytes, declared: str) -> str:
    """The icon's image type, from its bytes when the server's type is not an image's."""
    declared = declared.split(";", 1)[0].strip().lower()
    if declared.startswith("image/"):
        return declared
    for signature, media_type in _SIGNATURES:
        if body.startswith(signature):
            return media_type
    if body[:4] == b"RIFF" and body[8:12] == b"WEBP":
        return "image/webp"
    head = body[:256].lstrip().lower()
    if head.startswith(b"<svg") or (head.startswith(b"<?xml") and b"<svg" in body[:1024]):
        return "image/svg+xml"
    return ""


async def refresh(record: PageRecord) -> None:
    """Read the icon of the page ``record`` shows now, unless it is the one it has."""
    if urlsplit(record.url()).scheme not in _WEB:
        record.favicon = record.favicon_src = ""
        return
    try:
        href = str(await record.page.evaluate(_FIND_JS) or "")
    except errors(RuntimeError):  # the page navigated or closed meanwhile
        return
    if href == record.favicon_src:
        return
    record.favicon_src = href
    record.favicon = await _fetch(record, href)


async def _fetch(record: PageRecord, href: str) -> str:
    scheme = urlsplit(href).scheme
    if scheme == "data":
        return href if href.startswith("data:image/") and len(href) <= MAX_BYTES * 2 else ""
    if scheme not in _WEB:
        return ""
    try:
        response = await record.page.context.request.get(
            href, timeout=_TIMEOUT_MS, max_redirects=3, fail_on_status_code=False
        )
        if not response.ok:
            await response.dispose()
            return ""
        body = await response.body()
        declared = response.headers.get("content-type", "")
        await response.dispose()
    except errors(RuntimeError, OSError) as exc:  # no icon is a normal answer
        trace.event("BROWSER", "favicon unavailable page=%s error=%s", record.id, exc)
        return ""
    if not body or len(body) > MAX_BYTES:
        return ""
    media_type = _media_type(body, declared)
    if not media_type:
        return ""
    return f"data:{media_type};base64,{base64.b64encode(body).decode('ascii')}"
