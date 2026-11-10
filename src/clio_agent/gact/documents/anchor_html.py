"""HTML and SVG anchors: dragged regions and picked elements, resolved in a server render.

The version is rendered on the server, offline (``html_render``); the crop comes from that
render, never from the person's screen. A region lists the text inside
it and the elements it covers, with each element's tag and accessible name; a picked element
is found again by its selector and must match exactly one element.

An HTML region's ``rect`` is normalized to the whole document (``natural_width`` by
``natural_height`` CSS pixels at capture); an SVG region's to the drawing's own box.
"""

from __future__ import annotations

import io
import re
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from clio_agent.gact.documents.anchors import (
    AnchorState,
    MarkTarget,
    ResolvedMark,
    register,
)
from clio_agent.gact.documents.comment_settings import html_excerpt_chars
from clio_agent.gact.documents.html_render import HtmlRender, HtmlRenderError, render_html
from clio_agent.gact.documents.mark_images import png_bytes
from clio_agent.gact.documents.models import DocumentAnchor

if TYPE_CHECKING:
    from PIL import Image

__all__ = ["DomResolver", "register_builtin"]

_PROLOG = re.compile(r"(?is)^\s*(<\?xml[^>]*\?>\s*)?(<!doctype[^>]*>\s*)?")

_REFERENCE_JS = """(svg) => {
  if (svg) {
    const root = document.querySelector('svg');
    if (!root) return null;
    const r = root.getBoundingClientRect();
    return [r.left + scrollX, r.top + scrollY, r.width, r.height];
  }
  const d = document.documentElement;
  return [0, 0, d.scrollWidth, d.scrollHeight];
}"""

_UNDER_BOX_JS = """(box) => {
  const path = (el) => {
    if (el.id) return '#' + CSS.escape(el.id);
    const parts = [];
    for (let node = el; node && node.nodeType === 1 && node !== document.body; node = node.parentElement) {
      if (node.id) { parts.unshift('#' + CSS.escape(node.id)); break; }
      const tag = node.tagName.toLowerCase();
      const same = node.parentElement
        ? [...node.parentElement.children].filter((c) => c.tagName === node.tagName) : [];
      parts.unshift(same.length > 1 ? `${tag}:nth-of-type(${same.indexOf(node) + 1})` : tag);
    }
    return parts.join(' > ');
  };
  const within = (r) => {
    const left = r.left + scrollX, top = r.top + scrollY;
    return left >= box[0] - 1 && top >= box[1] - 1
      && left + r.width <= box[0] + box[2] + 1 && top + r.height <= box[1] + box[3] + 1;
  };
  const texts = [];
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  while (walker.nextNode()) {
    const node = walker.currentNode;
    const text = (node.textContent || '').replace(/\\s+/g, ' ').trim();
    const parent = node.parentElement;
    if (!text || !parent || ['SCRIPT', 'STYLE', 'TEMPLATE'].includes(parent.tagName)) continue;
    const range = document.createRange();
    range.selectNodeContents(node);
    const r = range.getBoundingClientRect();
    if (r.width && r.height && within(r)) texts.push(text);
  }
  const notable = /^(a|button|input|select|textarea|img|table|h[1-6]|figure|svg|canvas|g|text|path|rect|circle|line|polyline|polygon|ellipse|label|li|p)$/;
  const picks = [];
  for (const el of document.body.querySelectorAll('*')) {
    if (picks.length >= 40) break;
    const r = el.getBoundingClientRect();
    if (!r.width && !r.height) continue;
    if (!within(r)) continue;
    const tag = el.tagName.toLowerCase();
    if (!(el.id || el.getAttribute('role') || el.getAttribute('aria-label') || notable.test(tag))) continue;
    if (picks.some((p) => p.el.contains(el)) && !el.id) continue;
    picks.push({ el, tag, id: el.id || '', selector: path(el),
      text: (el.textContent || '').replace(/\\s+/g, ' ').trim().slice(0, 160) });
  }
  return { texts, picks: picks.map(({ el, ...rest }) => rest) };
}"""

_ELEMENT_JS = """([selector, limit]) => {
  let all;
  try { all = document.querySelectorAll(selector); } catch (error) { return { count: -1 }; }
  if (!all.length) return { count: 0 };
  const el = all[0];
  const r = el.getBoundingClientRect();
  return { count: all.length, tag: el.tagName.toLowerCase(), id: el.id || '',
    text: (el.textContent || '').replace(/\\s+/g, ' ').trim().slice(0, 400),
    html: el.outerHTML.slice(0, limit),
    rect: [r.left + scrollX, r.top + scrollY, r.width, r.height] };
}"""

_FIND_BY_TEXT_JS = """([tag, text]) => {
  const want = text.replace(/\\s+/g, ' ').trim();
  const hits = [...document.querySelectorAll(tag)].filter(
    (el) => (el.textContent || '').replace(/\\s+/g, ' ').trim() === want);
  if (hits.length !== 1) return null;
  const el = hits[0];
  return el.id ? '#' + CSS.escape(el.id) : null;
}"""


def _document(target: MarkTarget, svg: bool) -> str:
    text = target.path.read_bytes().decode("utf-8", errors="replace")
    if not svg:
        return text
    return (
        "<!doctype html><html><head><style>html,body{margin:0;background:#fff}</style></head>"
        f"<body>{_PROLOG.sub('', text, count=1)}</body></html>"
    )


def _crop(render: HtmlRender, shot: bytes, css_box: Sequence[float]) -> bytes:
    from PIL import Image  # noqa: PLC0415 - optional heavy import

    with Image.open(io.BytesIO(shot)) as image:
        scale = render.scale
        left, top, width, height = (value * scale for value in css_box)
        box = (
            max(0, int(left)),
            max(0, int(top)),
            min(image.width, int(left + width) + 1),
            min(image.height, int(top + height) + 1),
        )
        if box[2] <= box[0] or box[3] <= box[1]:
            return b""
        return png_bytes(image.crop(box).convert("RGB"))


def _element_label(tag: str, element_id: str) -> str:
    return f'<{tag} id="{element_id}">' if element_id else f"<{tag}>"


class DomResolver:
    """Regions and picked elements on HTML documents (``svg=False``) or SVG images."""

    def __init__(self, *, svg: bool) -> None:
        self.svg = svg

    def _render(self, target: MarkTarget, anchor: DocumentAnchor) -> Any:
        width = anchor.viewport_width or anchor.natural_width or 1280
        return render_html(_document(target, self.svg), viewport_width=width)

    def resolve(self, target: MarkTarget, anchor: DocumentAnchor) -> ResolvedMark:
        mode = "element" if anchor.profile == "dom" else "region"
        mark = ResolvedMark(mode=mode)  # type: ignore[arg-type]
        try:
            with self._render(target, anchor) as render:
                if anchor.profile == "dom":
                    self._element(render, anchor, mark)
                else:
                    self._region(render, anchor, mark)
                if render.refused:
                    mark.notes.append(
                        f"remote_resources_refused: {len(render.refused)} network requests were "
                        "not loaded in the server's offline render"
                    )
        except HtmlRenderError as exc:
            mark.notes.append(f"{exc.code}: {exc}")
            if anchor.profile == "dom":
                mark.lines.append(
                    ("Element", f"{_element_label(anchor.tag, '')}, selector {anchor.selector}")
                )
        return mark

    def _region(self, render: HtmlRender, anchor: DocumentAnchor, mark: ResolvedMark) -> None:
        reference = render.evaluate(_REFERENCE_JS, self.svg) or [0, 0, *render.document_size()]
        x, y, width, height = anchor.rect
        css_box = [
            reference[0] + x * reference[2],
            reference[1] + y * reference[3],
            width * reference[2],
            height * reference[3],
        ]
        mark.rect = (x, y, width, height)
        what = "the drawing" if self.svg else "the page"
        mark.lines.append(
            (
                "Box",
                f"x {round(x * 100)}% to {round((x + width) * 100)}%, "
                f"y {round(y * 100)}% to {round((y + height) * 100)}% of {what}",
            )
        )
        found = render.evaluate(_UNDER_BOX_JS, css_box) or {"texts": [], "picks": []}
        names = render.accessible_names([pick["selector"] for pick in found["picks"]])
        described: list[str] = []
        for pick, (role, name) in zip(found["picks"], names, strict=False):
            label = _element_label(pick["tag"], pick["id"])
            if role or name:
                label += f' {role} "{name}"' if name else f" {role}"
            elif pick["text"]:
                label += f' "{pick["text"][:80]}"'
            described.append(label)
        text = " ".join(found["texts"])
        under_parts = []
        if described:
            under_parts.append("; ".join(described))
        if text:
            under_parts.append(f'text "{text}"')
        mark.under = "; ".join(under_parts)
        if mark.under:
            mark.lines.append(("Under", mark.under))
        crop = _crop(render, render.screenshot(), css_box)
        if crop:
            mark.crop = crop

    def _element(self, render: HtmlRender, anchor: DocumentAnchor, mark: ResolvedMark) -> None:
        found = render.evaluate(_ELEMENT_JS, [anchor.selector, html_excerpt_chars()])
        count = int(found.get("count", 0)) if found else 0
        if count <= 0:
            mark.lines.append(
                ("Element", f"{_element_label(anchor.tag, '')}, selector {anchor.selector}")
            )
            mark.notes.append(
                "element_not_found: the selector matches nothing in this version"
                if count == 0
                else "element_selector_invalid: the selector does not parse"
            )
            if anchor.text_excerpt:
                mark.lines.append(("Text", f'"{anchor.text_excerpt}"'))
            return
        label = _element_label(found["tag"], found["id"])
        role, name = render.accessible_names([anchor.selector])[0]
        if role or name:
            label += f', role {role} "{name}"' if name else f", role {role}"
        mark.lines.append(("Element", f"{label}, selector {anchor.selector}"))
        if count > 1:
            mark.notes.append(
                f"element_ambiguous: the selector matches {count} elements; the first is shown"
            )
        if found["text"]:
            mark.lines.append(("Text", f'"{found["text"]}"'))
        if found["html"]:
            mark.lines.append(("HTML", found["html"]))
        left, top, width, height = found["rect"]
        mark.lines.append(
            (
                "Box",
                f"{round(width)} x {round(height)} px at ({round(left)}, {round(top)}) "
                f"in a {anchor.viewport_width or 1280} px wide view",
            )
        )
        mark.under = found["text"]
        reference = render.evaluate(_REFERENCE_JS, self.svg)
        if reference and reference[2] and reference[3]:
            mark.rect = (
                max(0.0, (left - reference[0]) / reference[2]),
                max(0.0, (top - reference[1]) / reference[3]),
                min(1.0, width / reference[2]),
                min(1.0, height / reference[3]),
            )
        crop = _crop(render, render.screenshot(), found["rect"])
        if crop:
            mark.crop = crop

    def page_image(self, target: MarkTarget, page_index: int | None) -> "Image.Image | None":
        cached = target.cache.get("dom_page_image")
        if cached is not None:
            return cached
        from PIL import Image  # noqa: PLC0415 - optional heavy import

        width = int(target.cache.get("viewport_width") or 1280)
        try:
            with render_html(_document(target, self.svg), viewport_width=width) as render:
                shot = render.screenshot()
                reference = render.evaluate(_REFERENCE_JS, self.svg)
        except HtmlRenderError:
            return None
        image = Image.open(io.BytesIO(shot)).convert("RGB")
        if self.svg and reference:
            scale = 2.0
            left, top, w, h = (value * scale for value in reference)
            image = image.crop((int(left), int(top), int(left + w), int(top + h)))
        target.cache["dom_page_image"] = image
        return image

    def reanchor(
        self, old: MarkTarget, new: MarkTarget, anchor: DocumentAnchor
    ) -> tuple[AnchorState, DocumentAnchor]:
        if anchor.profile == "region":
            return "current", anchor
        try:
            with self._render(new, anchor) as render:
                found = render.evaluate(_ELEMENT_JS, [anchor.selector, html_excerpt_chars()])
                if found and found.get("count") == 1:
                    return "moved", anchor
                if anchor.tag and anchor.text_excerpt:
                    selector = render.evaluate(_FIND_BY_TEXT_JS, [anchor.tag, anchor.text_excerpt])
                    if selector:
                        return "moved", anchor.model_copy(update={"selector": selector})
        except HtmlRenderError:
            return "outdated", anchor
        return "outdated", anchor


def register_builtin() -> None:
    """Regions and picked elements on HTML documents and SVG images."""

    register(("html-static",), ("region", "dom"), DomResolver(svg=False))
