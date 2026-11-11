"""The comment picker in CLIO's managed browser pages (comments plan, "Pages in the in-app browser").

The person picks an element or selects text on a page through the live view. A picker script is
installed in every page of a managed profile (``context.add_init_script``) and reports through a
binding (``context.expose_binding``); the live view starts and stops a pick with
``pick.start`` / ``pick.stop`` messages and receives each pick as a ``pick`` message. A page's own
script can call the binding too, so a report is accepted only while a pick of that kind is active
on that page, it is bounded, and the element it names is found again by its selector on the server
when the comment is sent (``browser/comments.py``). The ``cdp`` backend's real Chrome window has no
picker; its live view in CLIO does, since the picker lives in the page.

An element pick carries its selector, its accessibility target (role and name) and, for an image,
its ``src``; a text pick its quote and the text around it. The live view follows its unsent marks
with ``marks.watch``: the page finds each mark again (by selector, image source, role and name or
text for an element; by the quote and its context for text), reports where it is now as a
``marks`` message whenever the page scrolls, resizes or changes, and is handed the marks again after
every navigation, so the pins come back after a reload or on going back. A region is followed too:
the first time the page sees it, it holds the element under the region's centre and the region's
offset from it, so the region moves with that element when the layout shifts or the page scrolls.
Only marks made on the page's current address are handed to it. A page could forge a ``marks`` report too; it only moves
the person's pins, and the comment is resolved again on the server when it is sent.
"""

from __future__ import annotations

import logging
import math
import re
from collections.abc import Callable
from typing import Any

from clio_agent.gact.browser.settings import page_errors
from clio_agent.runtime import trace

__all__ = ["LOCATE_JS", "handle", "install", "release", "stop"]

logger = logging.getLogger(__name__)

_BINDING = "__clioCommentPick"
_MAX_TEXT = 4_000
_MAX_HTML = 8_000
_MAX_SRC = 2_000
_MAX_WATCHED = 50
_LABEL = re.compile(r"^S[1-9][0-9]{0,3}$")
_HOWS = frozenset({"selector", "src", "role", "text", "quote", "held", "region"})
_REASONS = frozenset({"missing", "changed", "hidden"})
_MODES: dict[str, str] = {}
_SINKS: dict[str, Callable[[dict[str, Any]], None]] = {}
_WATCH: dict[str, list[dict[str, Any]]] = {}
_WATCH_SINKS: dict[str, Callable[[dict[str, Any]], None]] = {}

# Shared by the picker in the page and the server's check when a comment is sent: how an
# element is named (selector, role, accessible name, image source) and how a mark is found again.
_HELPERS_JS = r"""
const clioSquash = (value) => String(value || '').replace(/\s+/g, ' ');
const clioOwn = (node) => Boolean(node && node.closest && node.closest('[data-clio-picker]'));
const clioPath = (el) => {
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
  // The page's root element has no path below it: it is named by its tag.
  return parts.length ? parts.join(' > ') : el.tagName.toLowerCase();
};
const CLIO_ROLES = {ARTICLE: 'article', BUTTON: 'button', DIALOG: 'dialog', FORM: 'form', H1: 'heading',
  H2: 'heading', H3: 'heading', H4: 'heading', H5: 'heading', H6: 'heading', IMG: 'img', LI: 'listitem',
  MAIN: 'main', NAV: 'navigation', OL: 'list', OPTION: 'option', P: 'paragraph', SELECT: 'combobox',
  TABLE: 'table', TD: 'cell', TEXTAREA: 'textbox', TH: 'columnheader', TR: 'row', UL: 'list'};
const CLIO_INPUTS = {button: 'button', checkbox: 'checkbox', image: 'button', radio: 'radio',
  range: 'slider', reset: 'button', search: 'searchbox', submit: 'button'};
const CLIO_NAMED = new Set(['A', 'BUTTON', 'CAPTION', 'H1', 'H2', 'H3', 'H4', 'H5', 'H6', 'LABEL',
  'LEGEND', 'LI', 'OPTION', 'SUMMARY', 'TD', 'TH']);
const clioRole = (el) => {
  const explicit = (el.getAttribute('role') || '').trim().split(/\s+/)[0];
  if (explicit) return explicit;
  if (el.tagName === 'A') return el.hasAttribute('href') ? 'link' : '';
  if (el.tagName === 'INPUT') return CLIO_INPUTS[(el.type || 'text').toLowerCase()] || 'textbox';
  return CLIO_ROLES[el.tagName] || '';
};
const clioName = (el) => {
  const clean = (value) => clioSquash(value).trim().slice(0, 200);
  const label = el.getAttribute('aria-label');
  if (label && label.trim()) return clean(label);
  const by = (el.getAttribute('aria-labelledby') || '').split(/\s+/).map((id) => id && document.getElementById(id)).filter(Boolean);
  if (by.length) return clean(by.map((node) => node.textContent).join(' '));
  if (el.tagName === 'IMG') return clean(el.getAttribute('alt'));
  if (el.labels && el.labels.length) return clean(el.labels[0].textContent);
  if (CLIO_NAMED.has(el.tagName)) return clean(el.innerText || el.textContent);
  return clean(el.getAttribute('title') || el.getAttribute('placeholder'));
};
const clioText = (el) => clioSquash(el.innerText || el.textContent).trim().slice(0, 4000);
const clioSrc = (el) => el.tagName === 'IMG' ? String(el.currentSrc || el.src || '').slice(0, 2000) : '';
const clioOne = (list) => list.length === 1 ? list[0] : null;
const clioElement = (anchor) => {
  const ax = (anchor.source && anchor.source.ax) || {};
  const text = clioSquash(anchor.text_excerpt).trim().slice(0, 200);
  const src = anchor.src || '';
  const named = (el) => Boolean(ax.name) && clioRole(el) === (ax.role || '') && clioName(el) === ax.name;
  const same = (el) => {
    if (src) return clioSrc(el) === src;
    // A unique id names its element whatever it now says.
    if (/^#[A-Za-z_][\w-]*$/.test(anchor.selector || '')) return true;
    if (named(el)) return true;
    if (text) return clioText(el).slice(0, 200) === text;
    return !ax.name;
  };
  let matched = [];
  try { matched = anchor.selector ? [...document.querySelectorAll(anchor.selector)] : []; } catch (error) { matched = []; }
  const bySelector = clioOne(matched.filter((el) => !clioOwn(el) && same(el)));
  if (bySelector) return [bySelector, 'selector'];
  const pool = [...document.getElementsByTagName(anchor.tag || '*')].filter((el) => !clioOwn(el)).slice(0, 5000);
  if (src) {
    const bySrc = clioOne(pool.filter((el) => clioSrc(el) === src));
    return bySrc ? [bySrc, 'src'] : [null, matched.length ? 'changed' : 'missing'];
  }
  const byRole = ax.name ? clioOne(pool.filter(named)) : null;
  if (byRole) return [byRole, 'role'];
  const byText = text ? clioOne(pool.filter((el) => clioText(el).slice(0, 200) === text)) : null;
  if (byText) return [byText, 'text'];
  return [null, matched.length ? 'changed' : 'missing'];
};
const clioIndex = () => {
  const index = {text: '', nodes: [], at: []};
  if (!document.body) return index;
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  let text = '';
  while (walker.nextNode()) {
    const node = walker.currentNode, parent = node.parentElement;
    if (!parent || ['SCRIPT', 'STYLE', 'NOSCRIPT', 'TEMPLATE'].includes(parent.tagName) || clioOwn(parent)) continue;
    const value = node.nodeValue || '';
    for (let i = 0; i < value.length; i++) {
      const space = /\s/.test(value[i]);
      if (space && text.endsWith(' ')) continue;
      text += space ? ' ' : value[i];
      index.nodes.push(node); index.at.push(i);
    }
  }
  index.text = text;
  return index;
};
const clioQuote = (anchor, index) => {
  const exact = clioSquash(anchor.exact).trim();
  if (!exact) return [null, 'missing'];
  const prefix = clioSquash(anchor.prefix), suffix = clioSquash(anchor.suffix);
  let best = -1, score = -1, tries = 0;
  for (let at = index.text.indexOf(exact); at >= 0 && tries < 500; at = index.text.indexOf(exact, at + 1), tries++) {
    const before = index.text.slice(Math.max(0, at - prefix.length), at);
    const after = index.text.slice(at + exact.length, at + exact.length + suffix.length);
    let s = 0, t = 0;
    while (s < before.length && before[before.length - 1 - s] === prefix[prefix.length - 1 - s]) s++;
    while (t < after.length && after[t] === suffix[t]) t++;
    if (s + t > score) { score = s + t; best = at; }
  }
  if (best < 0) return [null, 'missing'];
  const range = document.createRange(), last = best + exact.length - 1;
  range.setStart(index.nodes[best], index.at[best]);
  range.setEnd(index.nodes[last], index.at[last] + 1);
  return [range, 'quote'];
};
// A region follows what it was drawn over: the first time the page sees it, it holds the element
// under its centre and the region's offset from it, and finds that element again from then on.
// A region whose element is gone stays where it was drawn on the view.
const clioRegion = (anchor) => {
  const hold = (anchor.source && anchor.source.hold) || null;
  if (hold && hold.selector) {
    const [el] = clioElement({selector: hold.selector, tag: hold.tag});
    const b = el && el.getBoundingClientRect();
    if (b && (b.width || b.height))
      return {found: true, how: 'held', rect: [b.left + hold.dx, b.top + hold.dy, hold.width, hold.height]};
  }
  const r = Array.isArray(anchor.rect) && anchor.rect.length === 4 ? anchor.rect : null;
  if (!r) return {found: false, reason: 'missing'};
  const box = [r[0] * innerWidth, r[1] * innerHeight, r[2] * innerWidth, r[3] * innerHeight];
  const out = {found: true, how: 'region', rect: box};
  if (hold) return out;
  const el = document.elementFromPoint(box[0] + box[2] / 2, box[1] + box[3] / 2);
  if (el && !clioOwn(el) && el !== document.body && el !== document.documentElement) {
    const b = el.getBoundingClientRect();
    out.hold = {selector: clioPath(el), tag: el.tagName.toLowerCase(), dx: box[0] - b.left,
      dy: box[1] - b.top, width: box[2], height: box[3]};
  }
  return out;
};
const clioLocate = (items) => {
  let index = null;
  return (Array.isArray(items) ? items : []).slice(0, 50).map((item) => {
    const anchor = (item && item.anchor) || {}, label = String((item && item.label) || '');
    if (anchor.profile === 'region') return Object.assign({label}, clioRegion(anchor));
    let target = null, how = 'missing';
    try {
      if (anchor.profile === 'dom') [target, how] = clioElement(anchor);
      else if (anchor.profile === 'text-quote') { index = index || clioIndex(); [target, how] = clioQuote(anchor, index); }
    } catch (error) { target = null; how = 'missing'; }
    if (!target) return {label, found: false, reason: how};
    const r = target.getBoundingClientRect();
    if (!r.width && !r.height) return {label, found: false, reason: 'hidden'};
    const out = {label, found: true, how, rect: [r.left, r.top, r.width, r.height]};
    if (anchor.profile === 'dom' && how !== 'selector') out.selector = clioPath(target);
    return out;
  });
};
"""

# Run on the server by ``browser/comments.py``: where each ``{label, anchor}`` is on the page now.
LOCATE_JS = "(items) => {\n" + _HELPERS_JS + "\nreturn clioLocate(items);\n}"

_PICKER_JS = r"""
(() => {
  if (window.__clioCommentPicker) return;
  window.__clioCommentPicker = true;
  /*HELPERS*/
  let mode = '', box = null, watched = [], timer = 0, last = '', observing = false;
  const outline = (el) => {
    if (!box) {
      box = document.createElement('div');
      box.setAttribute('data-clio-picker', '');
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
    window.__clioCommentPick({kind: 'element', selector: clioPath(el), tag: el.tagName.toLowerCase(),
      text: clioText(el), html: el.outerHTML.slice(0, 8000), rect: [r.left, r.top, r.width, r.height],
      viewport: [innerWidth, innerHeight], src: clioSrc(el),
      alt: el.tagName === 'IMG' ? clioSquash(el.getAttribute('alt')).trim().slice(0, 400) : '',
      role: clioRole(el), name: clioName(el)});
  }, true);
  addEventListener('mouseup', () => {
    if (mode !== 'text') return;
    const selection = getSelection();
    if (!selection || selection.isCollapsed || !selection.rangeCount) return;
    const range = selection.getRangeAt(0), body = document.body;
    const before = document.createRange(); before.selectNodeContents(body); before.setEnd(range.startContainer, range.startOffset);
    const after = document.createRange(); after.selectNodeContents(body); after.setStart(range.endContainer, range.endOffset);
    const prefix = clioSquash(before.toString()), exact = clioSquash(range.toString()).trim();
    if (!exact) return;
    window.__clioCommentPick({kind: 'text', exact: exact.slice(0, 4000), prefix: prefix.slice(-64),
      suffix: clioSquash(after.toString()).slice(0, 64), start: prefix.length, end: prefix.length + exact.length});
    selection.removeAllRanges();
  }, true);
  // The marks the live view follows: found again whenever the page scrolls, resizes or changes.
  const observer = new MutationObserver((records) => {
    if (records.some((record) => !clioOwn(record.target.nodeType === 1 ? record.target : record.target.parentElement))) soon();
  });
  const report = () => {
    timer = 0;
    if (!watched.length || !document.body) return;
    if (!observing) {
      observer.observe(document.documentElement, {subtree: true, childList: true, characterData: true,
        attributes: true, attributeFilter: ['class', 'style', 'hidden', 'src', 'open']});
      observing = true;
    }
    const marks = clioLocate(watched);
    const key = JSON.stringify([innerWidth, innerHeight, marks]);
    if (key === last) return;
    last = key;
    window.__clioCommentPick({kind: 'marks', viewport: [innerWidth, innerHeight], marks});
  };
  const soon = () => { if (watched.length && !timer) timer = setTimeout(report, 120); };
  window.__clioWatchMarks = (list) => {
    watched = Array.isArray(list) ? list.slice(0, 50) : [];
    last = '';
    if (!watched.length) { observer.disconnect(); observing = false; return; }
    report();
  };
  addEventListener('scroll', soon, {capture: true, passive: true});
  addEventListener('resize', soon);
  addEventListener('load', soon);
  document.addEventListener('DOMContentLoaded', soon);
})();
""".replace("/*HELPERS*/", _HELPERS_JS)


def _text(value: Any, limit: int) -> str:
    return value[:limit] if isinstance(value, str) else ""


def _numbers(value: Any, count: int) -> list[float] | None:
    if not isinstance(value, list) or len(value) != count:
        return None
    if not all(isinstance(item, (int, float)) and not isinstance(item, bool) for item in value):
        return None
    return [float(item) for item in value]


def _hold(value: Any) -> dict[str, Any] | None:
    """A region's held element (selector, tag, and the region's offset and size from it in CSS
    pixels), bounded, or ``None`` when absent or malformed."""

    if not isinstance(value, dict):
        return None
    selector = _text(value.get("selector"), 1_000)
    numbers = _numbers([value.get(key) for key in ("dx", "dy", "width", "height")], 4)
    if not selector or numbers is None or not all(math.isfinite(n) for n in numbers):
        return None
    dx, dy, width, height = numbers
    tag = _text(value.get("tag"), 64)
    return {"selector": selector, "tag": tag, "dx": dx, "dy": dy, "width": width, "height": height}


def _page_url(url: str) -> str:
    """An address without its fragment: a mark made on it belongs to the whole document."""
    return url.split("#", 1)[0]


def _anchor(record: Any, payload: dict[str, Any]) -> dict[str, Any] | None:
    """A bounded ``DocumentAnchor``-shaped dict for a report, or ``None`` when malformed."""

    page = {"id": record.id, "url": record.url(), "title": record.title}
    if payload.get("kind") == "element":
        selector = _text(payload.get("selector"), 1_000)
        rect = _numbers(payload.get("rect"), 4)
        viewport = _numbers(payload.get("viewport"), 2)
        if not selector or rect is None or viewport is None:
            return None
        source: dict[str, Any] = {"page": page}
        role, name = _text(payload.get("role"), 64), _text(payload.get("name"), 200)
        if role or name:
            source["ax"] = {"role": role, "name": name}
        anchor = {
            "profile": "dom",
            "selector": selector,
            "tag": _text(payload.get("tag"), 64),
            "text_excerpt": _text(payload.get("text"), _MAX_TEXT)
            or _text(payload.get("alt"), _MAX_TEXT),
            "html_excerpt": _text(payload.get("html"), _MAX_HTML),
            "rect": rect,
            "viewport_width": int(viewport[0]),
            "natural_width": int(viewport[0]),
            "natural_height": int(viewport[1]),
            "source": source,
        }
        src = _text(payload.get("src"), _MAX_SRC)
        if src:
            anchor["src"] = src
        return anchor
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


def _watched(raw: Any) -> list[dict[str, Any]]:
    """The marks a live view follows: each label, the address it was made on and the parts of
    its anchor the page needs to find it again: an element's, a text selection's, or a region's
    rectangle and the element it holds to."""

    if not isinstance(raw, list):
        raise ValueError("marks must be a list")
    marks: list[dict[str, Any]] = []
    for item in raw[:_MAX_WATCHED]:
        if not isinstance(item, dict):
            continue
        label, anchor = item.get("label"), item.get("anchor")
        if not isinstance(label, str) or not _LABEL.match(label) or not isinstance(anchor, dict):
            continue
        if anchor.get("profile") not in ("dom", "text-quote", "region"):
            continue
        source = anchor.get("source") if isinstance(anchor.get("source"), dict) else {}
        page = source.get("page") if isinstance(source.get("page"), dict) else {}
        ax = source.get("ax") if isinstance(source.get("ax"), dict) else {}
        rect = _numbers(anchor.get("rect"), 4) if anchor["profile"] == "region" else None
        if anchor["profile"] == "region" and rect is None:
            continue
        hold = _hold(source.get("hold"))
        marks.append(
            {
                "label": label,
                "url": _page_url(_text(page.get("url"), 4_000)),
                "anchor": {
                    "profile": anchor["profile"],
                    "selector": _text(anchor.get("selector"), 1_000),
                    "tag": _text(anchor.get("tag"), 64),
                    "text_excerpt": _text(anchor.get("text_excerpt"), _MAX_TEXT),
                    "src": _text(anchor.get("src"), _MAX_SRC),
                    "exact": _text(anchor.get("exact"), _MAX_TEXT),
                    "prefix": _text(anchor.get("prefix"), 64),
                    "suffix": _text(anchor.get("suffix"), 64),
                    "rect": rect or [],
                    "source": {
                        "ax": {
                            "role": _text(ax.get("role"), 64),
                            "name": _text(ax.get("name"), 200),
                        },
                        **({"hold": hold} if hold else {}),
                    },
                },
            }
        )
    return marks


def _on_marks(record: Any, payload: dict[str, Any]) -> None:
    """Forward where the page found the followed marks, bounded, to the live view."""

    sink = _WATCH_SINKS.get(record.id)
    labels = {mark["label"] for mark in _WATCH.get(record.id, [])}
    viewport = _numbers(payload.get("viewport"), 2)
    raw = payload.get("marks")
    if sink is None or not labels or viewport is None or not isinstance(raw, list):
        trace.event("BROWSER", "browser marks ignored page=%s", record.id)
        return
    marks: list[dict[str, Any]] = []
    for item in raw[:_MAX_WATCHED]:
        if not isinstance(item, dict) or item.get("label") not in labels:
            continue
        if item.get("found") is True:
            rect = _numbers(item.get("rect"), 4)
            if rect is None or item.get("how") not in _HOWS:
                continue
            entry: dict[str, Any] = {
                "label": item["label"],
                "found": True,
                "how": item["how"],
                "rect": rect,
            }
            selector = _text(item.get("selector"), 1_000)
            if selector:
                entry["selector"] = selector
            hold = _hold(item.get("hold"))
            if hold:
                entry["hold"] = hold
        else:
            reason = item.get("reason") if item.get("reason") in _REASONS else "missing"
            entry = {"label": item["label"], "found": False, "reason": reason}
        marks.append(entry)
    sink({"type": "marks", "url": record.url(), "viewport": viewport, "marks": marks})


def _on_pick(source: dict[str, Any], payload: Any) -> None:
    from clio_agent.gact.browser.service import service  # noqa: PLC0415 - import cycle

    page = source.get("page")
    record = next((r for r in service().pages.values() if r.page is page), None)
    if record is None or not isinstance(payload, dict):
        trace.event("BROWSER", "browser pick ignored reason=unknown_page")
        return
    if payload.get("kind") == "marks":
        _on_marks(record, payload)
        return
    mode = _MODES.get(record.id)
    if mode is None or payload.get("kind") != mode:
        # The page's own script can call the binding; only an active pick is accepted.
        trace.event("BROWSER", "browser pick ignored reason=no_active_pick page=%s", record.id)
        return
    anchor = _anchor(record, payload)
    sink = _SINKS.get(record.id)
    if anchor is None or sink is None:
        reason = "malformed" if anchor is None else "no_viewer"
        trace.event("BROWSER", "browser pick ignored reason=%s page=%s", reason, record.id)
        return
    trace.event("BROWSER", "browser pick page=%s kind=%s", record.id, mode)
    sink({"type": "pick", "anchor": anchor})


async def install(context: Any) -> None:
    """Install the picker in every page of a managed profile's context, once."""

    if getattr(context, "_clio_comment_picker", False):
        return
    await context.expose_binding(_BINDING, _on_pick)
    await context.add_init_script(_PICKER_JS)
    context._clio_comment_picker = True


async def _push(record: Any) -> None:
    """Hand the page the followed marks made on its current address (none made elsewhere)."""

    here = _page_url(record.url())
    marks = [
        {"label": mark["label"], "anchor": mark["anchor"]}
        for mark in _WATCH.get(record.id, [])
        if mark["url"] == here
    ]
    try:
        await record.page.evaluate(
            "m => window.__clioWatchMarks && window.__clioWatchMarks(m)", marks
        )
    except page_errors() as exc:  # the page is navigating: the next navigation hands them again
        logger.info("browser marks push failed page=%s reason=%s", record.id, str(exc)[:200])


def _follow(record: Any) -> None:
    """Hand the page its marks again after each navigation of its main frame (a reload,
    going back, or a script changing the address), once per browser page."""

    page = record.page
    if getattr(page, "_clio_marks_followed", False):
        return

    async def navigated(frame: Any) -> None:
        if frame is page.main_frame and record.page is page and record.id in _WATCH:
            await _push(record)

    page.on("framenavigated", navigated)
    page._clio_marks_followed = True


async def handle(
    record: Any, message: dict[str, Any], deliver: Callable[[dict[str, Any]], None]
) -> bool:
    """Start or stop a pick for ``pick.start`` / ``pick.stop`` and follow the view's marks for
    ``marks.watch``; ``False`` for other messages."""

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
    if kind == "marks.watch":
        marks = _watched(message.get("marks"))
        if marks:
            _WATCH[record.id] = marks
            _WATCH_SINKS[record.id] = deliver
            _follow(record)
        else:
            _WATCH.pop(record.id, None)
            _WATCH_SINKS.pop(record.id, None)
        trace.event("BROWSER", "browser marks watch page=%s marks=%d", record.id, len(marks))
        await _push(record)
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


async def release(record: Any) -> None:
    """The controlling view left: end its pick and stop following its marks."""

    await stop(record)
    if _WATCH.pop(record.id, None) is None:
        return
    _WATCH_SINKS.pop(record.id, None)
    if not record.closed:
        await _push(record)
