"""What the agent receives for comments on a page in the in-app browser (comments plan).

A page comment is an attachment to the next message (ledger FB19). When it is sent, the server
takes a fresh screenshot of the page's view (addendum A, section 3) and resolves each mark on
the live page: a region is its box, the text and elements under it, and its crop; a picked
element is found again by its selector and named by Chromium's accessibility tree, which gives
a target spec the agent's browser script can use directly; a text selection carries its
context. The entries use the same format as every other comment session.
"""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from clio_agent.gact.documents.anchors import ResolvedMark, mark_mode
from clio_agent.gact.documents.comment_settings import context_chars
from clio_agent.gact.documents.mark_images import MarkBox, crop_png, labelled_overview_png
from clio_agent.gact.documents.review_render import entry_block, joined_labels

if TYPE_CHECKING:
    from fastapi import FastAPI

    from clio_agent.gact.documents.comment_attachments import CommentAttachment, RenderedAttachment

__all__ = ["render_page_comments"]

_UNDER_JS = """(box) => {
  const texts = [];
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  while (walker.nextNode()) {
    const node = walker.currentNode, parent = node.parentElement;
    const text = (node.textContent || '').replace(/\\s+/g, ' ').trim();
    if (!text || !parent || ['SCRIPT', 'STYLE'].includes(parent.tagName)) continue;
    const range = document.createRange(); range.selectNodeContents(node);
    const r = range.getBoundingClientRect();
    if (r.width && r.height && r.left >= box[0] - 1 && r.top >= box[1] - 1
        && r.right <= box[0] + box[2] + 1 && r.bottom <= box[1] + box[3] + 1) texts.push(text);
    if (texts.length >= 200) break;
  }
  return texts.join(' ');
}"""


async def _ax(page: Any, cdp: Any, selector: str) -> tuple[int, list[float] | None, str, str]:
    """How many elements ``selector`` matches, the first one's box, role and name."""

    locator = page.locator(selector)
    count = await locator.count()
    if not count:
        return 0, None, "", ""
    box = await locator.first.bounding_box()
    root = (await cdp.send("DOM.getDocument", {"depth": 0}))["root"]["nodeId"]
    node = await cdp.send("DOM.querySelector", {"nodeId": root, "selector": selector})
    role = name = ""
    if node.get("nodeId"):
        tree = await cdp.send(
            "Accessibility.getPartialAXTree", {"nodeId": node["nodeId"], "fetchRelatives": False}
        )
        first = (tree.get("nodes") or [{}])[0]
        role = str((first.get("role") or {}).get("value") or "")
        name = str((first.get("name") or {}).get("value") or "")
    rect = [box["x"], box["y"], box["width"], box["height"]] if box else None
    return count, rect, role, name


def _marked_view(attachment: "CommentAttachment") -> dict[str, int] | None:
    """The view size the person made the marks at (a region's or a pick's page view)."""

    for mark in attachment.marks:
        width, height = mark.anchor.natural_width, mark.anchor.natural_height
        if mark.anchor.profile in ("region", "dom") and width and height:
            return {"width": int(width), "height": int(height)}
    return None


def _capture(attachment: "CommentAttachment") -> Any:
    async def work(record: Any) -> dict[str, Any]:
        page = record.page
        shown = page.viewport_size or {"width": 1280, "height": 800}
        marked = _marked_view(attachment)
        # The live view lays the page out at its own size, which may have changed since the
        # marks were made (the canvas restored beside the conversation): the page is shown
        # at the marks' size for the screenshot, so the boxes fall where the person drew them.
        resized = marked is not None and marked != shown
        if resized:
            await page.set_viewport_size(marked)
            await page.wait_for_timeout(300)
        view = marked if resized else shown
        try:
            return await _measure(page, attachment, view)
        finally:
            if resized:
                await page.set_viewport_size(shown)

    return work


async def _measure(
    page: Any, attachment: "CommentAttachment", view: dict[str, int]
) -> dict[str, Any]:
    """The screenshot, and each mark resolved on the page at ``view``."""

    out: dict[str, Any] = {"png": await page.screenshot(type="png"), "view": view, "marks": {}}
    cdp = await page.context.new_cdp_session(page)
    try:
        for mark in attachment.marks:
            anchor = mark.anchor
            if anchor.profile == "dom" and anchor.selector:
                out["marks"][mark.label] = await _ax(page, cdp, anchor.selector)
            elif anchor.profile == "region" and anchor.rect:
                x, y, w, h = anchor.rect
                box = [
                    x * view["width"],
                    y * view["height"],
                    w * view["width"],
                    h * view["height"],
                ]
                out["marks"][mark.label] = await page.evaluate(_UNDER_JS, box)
    finally:
        await cdp.detach()
    return out


def _target_spec(role: str, name: str, selector: str) -> str:
    step = (
        {"by": "role", "role": role, "name": name}
        if role and name
        else {"by": "css", "css": selector}
    )
    return json.dumps({"steps": [step]})


def render_page_comments(
    app: "FastAPI", session_id: str, attachment: "CommentAttachment", folder: Path, images: bool
) -> "RenderedAttachment":
    """One page comment session as the agent receives it."""

    from PIL import Image  # noqa: PLC0415 - optional heavy import

    from clio_agent.gact.browser.service import service  # noqa: PLC0415
    from clio_agent.gact.browser.settings import BrowserError  # noqa: PLC0415
    from clio_agent.gact.documents.comment_attachments import RenderedAttachment  # noqa: PLC0415

    page_info = attachment.marks[0].anchor.source.get("page") or {}
    page_id = str(page_info.get("id") or "")
    header = f"Comment on page {page_id}, {page_info.get('url', '')}"
    if page_info.get("title"):
        header += f" ({page_info['title']})"
    svc = service()
    try:
        record = svc.owned(session_id, page_id)
        captured = svc.run(record, "screenshot", _capture(attachment))
    except BrowserError as exc:
        captured = None
        note = f"{exc.reason}: {exc.message}"
    screenshot = Image.open(io.BytesIO(captured["png"])).convert("RGB") if captured else None
    view = captured["view"] if captured else {"width": 1280, "height": 800}
    pictures: list[tuple[str, bytes]] = []
    resolved: dict[str, ResolvedMark] = {}
    boxes: list[MarkBox] = []
    for mark in attachment.marks:
        anchor = mark.anchor
        found = ResolvedMark(mode=mark_mode(anchor))
        if screenshot is None:
            found.notes.append(f"page_unavailable: {note}")
        if anchor.profile == "text-quote":
            width = context_chars()
            found.lines.append(("Selected", f'"{anchor.exact}"'))
            if anchor.prefix:
                found.lines.append(("Before", f'"...{anchor.prefix[-width:]}"'))
            if anchor.suffix:
                found.lines.append(("After", f'"{anchor.suffix[:width]}..."'))
            found.under = anchor.exact
        elif anchor.profile == "region" and anchor.rect:
            x, y, w, h = anchor.rect
            found.rect = (x, y, w, h)
            found.lines.append(
                (
                    "Box",
                    f"x {round(x * 100)}% to {round((x + w) * 100)}%, y {round(y * 100)}% to {round((y + h) * 100)}% of the page view",
                )
            )
            under = str((captured or {}).get("marks", {}).get(mark.label) or "")
            if under:
                found.under = under
                found.lines.append(("Under", f'text "{under}"'))
            if screenshot is not None:
                found.crop = crop_png(screenshot, anchor.rect)
        elif anchor.profile == "dom":
            count, rect, role, name = (captured or {}).get("marks", {}).get(mark.label) or (
                0,
                None,
                "",
                "",
            )
            label = f"<{anchor.tag or 'element'}>"
            if role:
                label += f", role {role}" + (f' "{name}"' if name else "")
            found.lines.append(("Element", label))
            found.lines.append(("Target", _target_spec(role, name, anchor.selector)))
            if anchor.text_excerpt:
                found.lines.append(("Text", f'"{anchor.text_excerpt[:400]}"'))
            if count == 0:
                found.notes.append(
                    "element_not_found: the selector matches nothing on the page now"
                )
            elif count > 1:
                found.notes.append(
                    f"element_ambiguous: the selector matches {count} elements; the first is shown"
                )
            if rect is not None:
                left, top, width_px, height_px = rect
                found.lines.append(
                    (
                        "Box",
                        f"{round(width_px)} x {round(height_px)} px at ({round(left)}, {round(top)}) in a {view['width']} px wide view",
                    )
                )
                found.rect = (
                    max(0.0, left / view["width"]),
                    max(0.0, top / view["height"]),
                    min(1.0, width_px / view["width"]),
                    min(1.0, height_px / view["height"]),
                )
                if screenshot is not None:
                    found.crop = crop_png(screenshot, found.rect)
        resolved[mark.label] = found
        if found.rect is not None:
            boxes.append(MarkBox(mark.label, found.rect))
    if screenshot is not None and boxes:
        overview = labelled_overview_png(screenshot, boxes)
        (folder / "overview.png").write_bytes(overview)
        pictures.append(
            (f"the page with {joined_labels([box.label for box in boxes])} drawn on it", overview)
        )
    numbers: dict[str, int] = {}
    for mark in attachment.marks:
        crop = resolved[mark.label].crop
        if crop:
            (folder / f"{mark.label}.png").write_bytes(crop)
            pictures.append((f"{mark.label} at twice its size", crop))
            numbers[mark.label] = len(pictures)
    lines = [header + "."]
    if pictures:
        if images:
            if boxes:
                lines.append(f"Image 1 is {pictures[0][0]}.")
        else:
            lines.append(
                "Images were not sent: the selected model does not accept images. The overview "
                f"and crops are saved in {folder}."
            )
    blocks = ["\n".join(lines)]
    for mark in attachment.marks:
        blocks.append(
            entry_block(
                mark.label,
                resolved[mark.label],
                mark.comment,
                image_number=numbers.get(mark.label) if images else None,
                overflow_dir=folder,
            )
        )
    return RenderedAttachment("\n\n".join(blocks), pictures if images else [])
