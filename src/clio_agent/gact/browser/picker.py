"""The comment picker in CLIO's managed browser pages (comments plan, "Pages in the in-app browser").

The person picks an element or selects text on a page through the live view. A picker script is
installed in every page of a managed profile (``context.add_init_script``) and reports through a
binding (``context.expose_binding``); the live view starts and stops a pick with
``pick.start`` / ``pick.stop`` messages and receives each pick as a ``pick`` message. A page's own
script can call the binding too, so a report is accepted only while a pick of that kind is active
on that page, it is bounded, and the element it names is found again by its selector on the server
when the comment is sent (``browser/comments.py``). The ``cdp`` backend's real Chrome window has no
picker; its live view in CLIO does, since the picker lives in the page.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from clio_agent.gact.browser.settings import page_errors

__all__ = ["handle", "install", "stop"]

logger = logging.getLogger(__name__)

_BINDING = "__clioCommentPick"
_MAX_TEXT = 4_000
_MAX_HTML = 8_000
_MODES: dict[str, str] = {}
_SINKS: dict[str, Callable[[dict[str, Any]], None]] = {}

_PICKER_JS = r"""
(() => {
  if (window.__clioCommentPicker) return;
  window.__clioCommentPicker = true;
  let mode = '', box = null;
  const path = (el) => {
    if (el.id && document.querySelectorAll('#' + CSS.escape(el.id)).length === 1) return '#' + CSS.escape(el.id);
    for (const name of ['data-testid', 'data-id', 'name', 'aria-label']) {
      const value = el.getAttribute && el.getAttribute(name);
      if (value) {
        const candidate = el.tagName.toLowerCase() + '[' + name + '="' + CSS.escape(value) + '"]';
        if (document.querySelectorAll(candidate).length === 1) return candidate;
      }
    }
    const parts = [];
    for (let node = el; node && node.nodeType === 1 && node !== document.documentElement; node = node.parentElement) {
      if (node.id) { parts.unshift('#' + CSS.escape(node.id)); break; }
      const tag = node.tagName.toLowerCase();
      const same = node.parentElement ? [...node.parentElement.children].filter((c) => c.tagName === node.tagName) : [];
      parts.unshift(same.length > 1 ? tag + ':nth-of-type(' + (same.indexOf(node) + 1) + ')' : tag);
      const selector = parts.join(' > ');
      if (document.querySelectorAll(selector).length === 1) return selector;
    }
    return parts.join(' > ');
  };
  const outline = (el) => {
    if (!box) {
      box = document.createElement('div');
      box.style.cssText = 'position:fixed;pointer-events:none;z-index:2147483647;outline:2px solid Highlight;outline-offset:1px';
      document.documentElement.appendChild(box);
    }
    const r = el.getBoundingClientRect();
    Object.assign(box.style, {display: 'block', left: r.left + 'px', top: r.top + 'px', width: r.width + 'px', height: r.height + 'px'});
  };
  window.__clioSetPickMode = (next) => { mode = next || ''; if (!mode && box) box.style.display = 'none'; };
  addEventListener('mouseover', (event) => { if (mode === 'element' && event.target instanceof Element) outline(event.target); }, true);
  addEventListener('click', (event) => {
    if (mode !== 'element' || !(event.target instanceof Element)) return;
    event.preventDefault(); event.stopPropagation();
    const el = event.target, r = el.getBoundingClientRect();
    window.__clioCommentPick({kind: 'element', selector: path(el), tag: el.tagName.toLowerCase(),
      text: (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 4000),
      html: el.outerHTML.slice(0, 8000), rect: [r.left, r.top, r.width, r.height],
      viewport: [innerWidth, innerHeight]});
  }, true);
  addEventListener('mouseup', () => {
    if (mode !== 'text') return;
    const selection = getSelection();
    if (!selection || selection.isCollapsed || !selection.rangeCount) return;
    const range = selection.getRangeAt(0), body = document.body;
    const before = document.createRange(); before.selectNodeContents(body); before.setEnd(range.startContainer, range.startOffset);
    const after = document.createRange(); after.selectNodeContents(body); after.setStart(range.endContainer, range.endOffset);
    const squash = (value) => value.replace(/\s+/g, ' ');
    const prefix = squash(before.toString()), exact = squash(range.toString()).trim();
    if (!exact) return;
    window.__clioCommentPick({kind: 'text', exact: exact.slice(0, 4000), prefix: prefix.slice(-64),
      suffix: squash(after.toString()).slice(0, 64), start: prefix.length, end: prefix.length + exact.length});
    selection.removeAllRanges();
  }, true);
})();
"""


def _text(value: Any, limit: int) -> str:
    return value[:limit] if isinstance(value, str) else ""


def _numbers(value: Any, count: int) -> list[float] | None:
    if not isinstance(value, list) or len(value) != count:
        return None
    if not all(isinstance(item, (int, float)) for item in value):
        return None
    return [float(item) for item in value]


def _anchor(record: Any, payload: dict[str, Any]) -> dict[str, Any] | None:
    """A bounded ``DocumentAnchor``-shaped dict for a report, or ``None`` when malformed."""

    page = {"id": record.id, "url": record.url(), "title": record.title}
    if payload.get("kind") == "element":
        selector = _text(payload.get("selector"), 1_000)
        rect = _numbers(payload.get("rect"), 4)
        viewport = _numbers(payload.get("viewport"), 2)
        if not selector or rect is None or viewport is None:
            return None
        return {
            "profile": "dom",
            "selector": selector,
            "tag": _text(payload.get("tag"), 64),
            "text_excerpt": _text(payload.get("text"), _MAX_TEXT),
            "html_excerpt": _text(payload.get("html"), _MAX_HTML),
            "rect": rect,
            "viewport_width": int(viewport[0]),
            "source": {"page": page},
        }
    exact = _text(payload.get("exact"), _MAX_TEXT).strip()
    if payload.get("kind") != "text" or not exact:
        return None
    return {
        "profile": "text-quote",
        "exact": exact,
        "prefix": _text(payload.get("prefix"), 64),
        "suffix": _text(payload.get("suffix"), 64),
        "source": {"page": page},
    }


def _on_pick(source: dict[str, Any], payload: Any) -> None:
    from clio_agent.gact.browser.service import service  # noqa: PLC0415 - import cycle

    page = source.get("page")
    record = next((r for r in service().pages.values() if r.page is page), None)
    if record is None or not isinstance(payload, dict):
        return
    mode = _MODES.get(record.id)
    if mode is None or payload.get("kind") != mode:
        # The page's own script can call the binding; only an active pick is accepted.
        logger.info("browser pick ignored reason=no_active_pick page=%s", record.id)
        return
    anchor = _anchor(record, payload)
    sink = _SINKS.get(record.id)
    if anchor is not None and sink is not None:
        sink({"type": "pick", "anchor": anchor})


async def install(context: Any) -> None:
    """Install the picker in every page of a managed profile's context, once."""

    if getattr(context, "_clio_comment_picker", False):
        return
    await context.expose_binding(_BINDING, _on_pick)
    await context.add_init_script(_PICKER_JS)
    context._clio_comment_picker = True


async def handle(
    record: Any, message: dict[str, Any], deliver: Callable[[dict[str, Any]], None]
) -> bool:
    """Start or stop a pick for ``pick.start`` / ``pick.stop``; ``False`` for other messages."""

    kind = message.get("type")
    if kind == "pick.start" and message.get("mode") in {"element", "text"}:
        _MODES[record.id] = str(message["mode"])
        _SINKS[record.id] = deliver
        await record.page.evaluate(
            "m => window.__clioSetPickMode && window.__clioSetPickMode(m)", _MODES[record.id]
        )
        return True
    if kind == "pick.stop":
        await stop(record)
        return True
    return False


async def stop(record: Any) -> None:
    """End any pick on ``record``'s page, so its clicks reach the page again."""

    _SINKS.pop(record.id, None)
    if _MODES.pop(record.id, None) is None or record.closed:
        return
    try:
        await record.page.evaluate("() => window.__clioSetPickMode && window.__clioSetPickMode('')")
    except page_errors() as exc:  # the page is gone or navigating
        logger.info("browser pick stop failed page=%s reason=%s", record.id, str(exc)[:200])
