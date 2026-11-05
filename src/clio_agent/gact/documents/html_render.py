"""Render an HTML or SVG version on the server, as the person's preview frame shows it.

The comment renderer crops from the version itself, never from the person's screen
(comments plan, "Region capture"; ledger FB14, FB15). The preview frame runs the page's
scripts with an opaque origin and loads only the chart libraries its policy names
(gact-tui ``html-interactive-policy.ts``). This render matches that: a short-lived Chromium of
the managed browser's executable (never the person's profile), scripts on, requests to the
same library origins allowed and every other request refused and counted, at the viewer's
width and twice its pixel density.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

__all__ = [
    "LIBRARY_ORIGINS",
    "HtmlRender",
    "HtmlRenderError",
    "render_html",
]

logger = logging.getLogger(__name__)

#: The origins the preview frame's policy lets a page load libraries from.
LIBRARY_ORIGINS = frozenset(
    {
        "https://cdn.jsdelivr.net",
        "https://unpkg.com",
        "https://esm.sh",
        "https://cdnjs.cloudflare.com",
        "https://cdn.babylonjs.com",
    }
)
_LOAD_TIMEOUT_MS = 20_000
_SETTLE_MS = 300
_SANDBOX_MARKERS = ("sandboxing failed", "no usable sandbox", "without --no-sandbox")


class HtmlRenderError(RuntimeError):
    """The server could not render the version; ``code`` is the typed reason."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass
class HtmlRender:
    """A loaded page and what the render refused to fetch."""

    page: Any
    scale: float
    refused: list[str] = field(default_factory=list)

    def evaluate(self, script: str, arg: Any = None) -> Any:
        """Run ``script`` (a function expression) in the page with ``arg``."""
        return self.page.evaluate(script, arg)

    def document_size(self) -> tuple[int, int]:
        """The document's full width and height in CSS pixels."""
        width, height = self.page.evaluate(
            "() => [document.documentElement.scrollWidth, document.documentElement.scrollHeight]"
        )
        return int(width), int(height)

    def screenshot(self) -> bytes:
        """The whole document as PNG, at the render's scale."""
        return bytes(self.page.screenshot(full_page=True, type="png"))

    def accessible_names(self, selectors: list[str]) -> list[tuple[str, str]]:
        """Role and accessible name of the first element each selector matches."""

        cdp = self.page.context.new_cdp_session(self.page)
        try:
            root = cdp.send("DOM.getDocument", {"depth": 0})["root"]["nodeId"]
            out: list[tuple[str, str]] = []
            for selector in selectors:
                node = cdp.send("DOM.querySelector", {"nodeId": root, "selector": selector})
                if not node.get("nodeId"):
                    out.append(("", ""))
                    continue
                tree = cdp.send(
                    "Accessibility.getPartialAXTree",
                    {"nodeId": node["nodeId"], "fetchRelatives": False},
                )
                first = (tree.get("nodes") or [{}])[0]
                out.append(
                    (
                        str((first.get("role") or {}).get("value") or ""),
                        str((first.get("name") or {}).get("value") or ""),
                    )
                )
            return out
        finally:
            cdp.detach()


def _executable() -> tuple[dict[str, Any], str]:
    """Launch options naming the managed browser's executable or channel."""

    from clio_agent.gact.browser import install, settings  # noqa: PLC0415 - lazy (install)
    from clio_agent.gact.browser.settings import BrowserError  # noqa: PLC0415

    product = settings.channel()
    if product != "chromium":
        return {"channel": product}, product
    try:
        return {"executable_path": str(install.ensure_chromium())}, product
    except BrowserError as exc:
        raise HtmlRenderError(exc.reason, exc.message) from exc


def _launch(pw: Any) -> Any:
    from clio_agent.gact.browser import settings  # noqa: PLC0415
    from clio_agent.gact.browser.service import _chromium_env  # noqa: PLC0415

    options, _product = _executable()
    # The browser service's environment: a short TMPDIR, since Chromium aborts when its
    # process-singleton socket path is too long ("Socket path too long").
    options["env"] = _chromium_env()
    try:
        return pw.chromium.launch(headless=True, chromium_sandbox=True, **options)
    except Exception as exc:  # noqa: BLE001 - classified into a typed refusal below
        text = str(exc)
        if not any(marker in text.lower() for marker in _SANDBOX_MARKERS):
            raise HtmlRenderError("html_render_launch_failed", text[-800:]) from exc
        if not settings.allow_unsandboxed():
            raise HtmlRenderError(
                "browser_sandbox_unavailable",
                "Chromium's sandbox could not start and browser.allow_unsandboxed is off",
            ) from exc
        logger.info("html render runs without the Chromium sandbox reason=sandbox_unavailable")
        return pw.chromium.launch(headless=True, chromium_sandbox=False, **options)


@contextmanager
def render_html(html: str, *, viewport_width: int, scale: float = 2.0) -> Iterator[HtmlRender]:
    """Load ``html`` in a fresh, isolated Chromium and yield the loaded page.

    Raises:
        HtmlRenderError: the browser runtime is missing or failed to start
            (``browser_runtime_*``, ``html_render_launch_failed``,
            ``browser_sandbox_unavailable``), or the page did not load
            (``html_render_load_failed``).
    """

    from playwright.sync_api import sync_playwright  # noqa: PLC0415 - lazy (install)

    with sync_playwright() as pw:
        browser = _launch(pw)
        try:
            context = browser.new_context(
                viewport={"width": max(200, int(viewport_width)), "height": 800},
                device_scale_factor=scale,
                java_script_enabled=True,
                service_workers="block",
            )
            render = HtmlRender(page=None, scale=scale)

            def gate(route: Any) -> None:
                url = route.request.url
                parts = urlsplit(url)
                if f"{parts.scheme}://{parts.netloc}" in LIBRARY_ORIGINS:
                    route.continue_()
                    return
                render.refused.append(url)
                route.abort()

            context.route("**/*", gate)
            page = context.new_page()
            try:
                page.set_content(html, wait_until="load", timeout=_LOAD_TIMEOUT_MS)
                page.wait_for_timeout(_SETTLE_MS)
            except Exception as exc:  # noqa: BLE001 - forwards: a typed load failure
                raise HtmlRenderError("html_render_load_failed", str(exc)[-800:]) from exc
            render.page = page
            yield render
        finally:
            browser.close()
