"""Headers for bytes CLIO serves inline: what a browser may do with them.

Workspace files, resources and artifacts are served on CLIO's own origin, and
their bytes can come from anyone: a cloned repository, a dataset, the model. A
browser that navigates to one of them (a link, or a redirect from any website)
renders it as a document of that origin when its type is HTML, XHTML, SVG or
XML, and its scripts could then call CLIO's API. So every inline response gets
``X-Content-Type-Options: nosniff``, and an active type also gets a CSP sandbox
that runs no script and loads nothing. Clients that fetch the bytes (the UI's
viewers) are unaffected: these headers only govern a navigated document.
"""

from __future__ import annotations

from typing import TypeVar

from starlette.responses import Response

_R = TypeVar("_R", bound=Response)

INLINE_SANDBOX_CSP = "sandbox; default-src 'none'; img-src data:; style-src 'unsafe-inline'"

_ACTIVE_TYPES = frozenset(
    {"text/html", "application/xhtml+xml", "image/svg+xml", "application/xml", "text/xml"}
)


def is_active_type(media_type: str | None) -> bool:
    """Whether a browser renders this type as a document that can run script."""
    essence = (media_type or "").split(";", 1)[0].strip().lower()
    return essence in _ACTIVE_TYPES or essence.endswith("+xml")


def inline_safe(response: _R, media_type: str | None = None) -> _R:
    """Add the inline-safety headers to ``response`` and return it."""
    response.headers["X-Content-Type-Options"] = "nosniff"
    if is_active_type(media_type or response.media_type):
        response.headers["Content-Security-Policy"] = INLINE_SANDBOX_CSP
    return response
