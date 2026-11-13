"""Comments on an MCP App view, rendered for the agent (CM3).

A view is not a versioned artifact, so its comments are an attachment of the next message
(comments plan, "Stored threads or attachments"). The browser makes the images, as it does
for an A2UI surface (addendum A, section 3): the view's pixels exist only in the person's
browser, and the snapshot CLIO's sandbox document takes there is the one the agent's
``look_at_view`` gets (:mod:`clio_agent.gact.mcp_app_snapshot_script`). Image 1 is the
view with every mark drawn; each mark's crop, at twice its size, follows.

Each mark carries, in ``anchor.source``, the view's identity (``app``: instance id,
server, tool, title), the context the view had last reported when the mark was made
(``context``, the ``ui/update-model-context`` params) and, for a clicked point, the point.
The entry turns these into readable lines: no anchor JSON reaches the model. A mark with
no context of its own reads the session's stored report of the view
(:mod:`clio_agent.gact.mcp_app_context`) and says so. A mark the browser could not picture
carries ``snapshot_refused`` and the entry states that reason instead of a crop; parts of
the view the snapshot could not paint (``snapshot_skipped``) are listed too.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from clio_agent.gact.documents.anchors import ResolvedMark
from clio_agent.gact.documents.comment_attachments import (
    CommentAttachment,
    RenderedAttachment,
    attachment_images,
)
from clio_agent.gact.documents.review_render import entry_block

if TYPE_CHECKING:
    from fastapi import FastAPI

__all__ = ["render_app_view_comments"]

#: One mark's context, at most, as encoded JSON (the view's own cap, MA10).
_MAX_CONTEXT_BYTES = 128 * 1024


def _app(attachment: CommentAttachment) -> dict[str, Any]:
    app = attachment.marks[0].anchor.source.get("app")
    return dict(app) if isinstance(app, dict) else {}


def _header(attachment: CommentAttachment) -> str:
    app = _app(attachment)
    title = str(app.get("title") or attachment.title or "an app view")
    server = str(app.get("server") or "")
    tool = str(app.get("tool") or "")
    detail = ", ".join(
        part
        for part in (
            f"{server} view" if server else "",
            f"opened by {tool}" if tool else "",
            f"id {app.get('app_instance_id')}" if app.get("app_instance_id") else "",
        )
        if part
    )
    return f"Comment on the view “{title}”{f' ({detail})' if detail else ''}."


def _box(anchor: Any) -> str:
    x, y, width, height = anchor.rect
    text = (
        f"x {round(x * 100)}% to {round((x + width) * 100)}%, "
        f"y {round(y * 100)}% to {round((y + height) * 100)}% of the view"
    )
    if anchor.natural_width and anchor.natural_height:
        text += (
            f" ({round(width * anchor.natural_width)}x{round(height * anchor.natural_height)} px "
            f"at ({round(x * anchor.natural_width)}, {round(y * anchor.natural_height)}) in a "
            f"{anchor.natural_width}x{anchor.natural_height} px view)"
        )
    return text


def _context(
    app: "FastAPI", session_id: str, attachment: CommentAttachment, source: dict[str, Any]
) -> tuple[list[str], str]:
    """The view's report for one mark, as lines, and where it came from."""
    from clio_agent.gact.mcp_app_context import stored_app_context  # noqa: PLC0415
    from clio_agent.gact.mcp_app_look import view_context_lines  # noqa: PLC0415

    context = source.get("context")
    encoded = json.dumps(context, default=str).encode("utf-8") if context else b""
    if context and len(encoded) <= _MAX_CONTEXT_BYTES:
        return view_context_lines(context), "when the mark was made"
    instance = str(_app(attachment).get("app_instance_id") or "")
    stored = stored_app_context(app, session_id, instance) if instance else None
    lines = view_context_lines((stored or {}).get("context"))
    if lines:
        return lines, "as the view last reported it (the mark carried none of its own)"
    return [], ""


def render_app_view_comments(
    app: "FastAPI",
    session_id: str,
    attachment: CommentAttachment,
    folder: Path,
    images: bool,
) -> RenderedAttachment:
    """Regions and points on an app view: the overview and crops the browser made, and per
    mark the view's report and what the picture could not show."""

    from clio_agent.gact.mcp_app_look import coverage_lines  # noqa: PLC0415

    labels = [mark.label for mark in attachment.marks]
    pictures, numbers = attachment_images(attachment, folder, labels)
    lines = [_header(attachment)]
    if pictures:
        if images:
            if not pictures[0][0].startswith("S"):
                lines.append(f"Image 1 is {pictures[0][0]}.")
        else:
            lines.append(
                "Images were not sent: the selected model does not accept images. The overview "
                f"and crops are saved in {folder}."
            )
    shared: list[str] | None = None
    blocks: list[str] = []
    for mark in attachment.marks:
        anchor = mark.anchor
        source = anchor.source
        resolved = ResolvedMark(mode="region")
        if anchor.rect:
            resolved.lines.append(("Box", _box(anchor)))
        point = source.get("point")
        if isinstance(point, list) and len(point) == 2:
            resolved.lines.append(
                ("Point", f"x {round(point[0] * 100)}%, y {round(point[1] * 100)}% of the view")
            )
        context, origin = _context(app, session_id, attachment, source)
        if context and context != shared:
            resolved.lines.append(("View", "\n".join([f"reported {origin}:", *context])))
            shared = context
        elif context:
            resolved.lines.append(("View", "as reported for the mark before"))
        else:
            resolved.notes.append("The view had reported no context.")
        refused = source.get("snapshot_refused")
        if isinstance(refused, dict):
            resolved.notes.append(
                f"No picture of this mark: {refused.get('message') or 'the snapshot failed'} "
                f"({refused.get('reason') or 'snapshot_failed'})."
            )
        elif mark.label not in numbers:
            resolved.notes.append("No picture of this mark was attached (snapshot_missing).")
        skipped = source.get("snapshot_skipped")
        if isinstance(skipped, list) and skipped:
            gaps = [
                line
                for line in coverage_lines({"skipped": skipped})
                if line.startswith("Not in the image")
            ]
            resolved.notes.extend(gaps)
        blocks.append(
            entry_block(
                mark.label,
                resolved,
                mark.comment,
                image_number=numbers.get(mark.label) if images else None,
                overflow_dir=folder,
            )
        )
    return RenderedAttachment("\n\n".join(["\n".join(lines), *blocks]), pictures if images else [])
