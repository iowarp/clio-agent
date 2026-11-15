"""Comments on a client-rendered file view, rendered for the agent (FV-D1, FV-D2).

A file such as a STEP or STL model is drawn by the person's browser (the file viewer's 3D
view); the server has its bytes but cannot draw what the person sees: the camera, the
section, the exploded parts, the measurements. So a region comment on such a view is an
attachment of the next message, as on an app view (CM3), not a stored thread resolved
from the file's bytes: the browser supplies the overview (every mark drawn) and a crop per
mark at twice its size, and each mark carries, in ``anchor.source``:

* ``file``: ``{workspace_id, path, name, kind}``, the file and the kind of view;
* ``view_state``: the view state when the mark was made, as readable lines the renderer
  wrote (camera direction, section plane and position, explode amount, selected part,
  visible parts, units, measurements);
* optionally ``point`` (a clicked point), ``snapshot_refused`` and ``snapshot_skipped``.

The server validates what the browser made before anything reaches the model, and refuses
with a typed reason (``invalid_comment_session`` 422 naming it), never a silent cut:
``file_view_identity_missing``/``_invalid``, ``file_view_mixed_files``,
``file_view_state_invalid``/``_too_large``, ``file_view_overview_repeated``,
``file_view_image_unmatched`` and ``file_view_image_too_large``. The images are stored
where every attachment's images go (the session's comment folder).
"""

from __future__ import annotations

import base64
import binascii
import io
from pathlib import Path
from typing import TYPE_CHECKING

from clio_agent.gact.documents.anchors import ResolvedMark
from clio_agent.gact.documents.app_view_comments import view_box
from clio_agent.gact.documents.comment_attachments import (
    CommentAttachment,
    CommentAttachmentError,
    RenderedAttachment,
    attachment_images,
)
from clio_agent.gact.documents.review_render import entry_block
from clio_agent.gact.file_views import FileViewInvalid, state_lines, view_file

if TYPE_CHECKING:
    from fastapi import FastAPI

__all__ = ["MAX_IMAGE_SIDE", "render_file_view_comments", "validate_file_view_attachment"]

#: The longest side of an overview or crop the browser may send, in pixels.
MAX_IMAGE_SIDE = 8192


def _refuse(reason: str, message: str) -> CommentAttachmentError:
    return CommentAttachmentError(f"{reason}: {message}")


def _image_side(data: str) -> int:
    from PIL import Image  # noqa: PLC0415

    try:
        raw = base64.b64decode(data.split(",", 1)[-1], validate=True)
        with Image.open(io.BytesIO(raw)) as image:
            return max(image.size)
    except (binascii.Error, ValueError, OSError) as exc:
        raise _refuse("file_view_image_invalid", "a comment image is not a PNG") from exc


def validate_file_view_attachment(attachment: CommentAttachment) -> dict[str, str]:
    """Check what the browser made for a file view session; return the file it shows.

    Raises:
        CommentAttachmentError: a typed reason (see the module docstring).
    """
    files = []
    try:
        for mark in attachment.marks:
            files.append(view_file(mark.anchor.source.get("file")))
            state_lines(mark.anchor.source.get("view_state"))
    except FileViewInvalid as exc:
        raise _refuse(exc.reason, exc.message) from exc
    first = files[0]
    if any(
        (item["workspace_id"], item["path"]) != (first["workspace_id"], first["path"])
        for item in files
    ):
        raise _refuse("file_view_mixed_files", "one comment session covers one file view")
    labels = {mark.label for mark in attachment.marks}
    overviews = [image for image in attachment.images if not image.label]
    if len(overviews) > 1:
        raise _refuse("file_view_overview_repeated", "a session has at most one overview")
    for image in attachment.images:
        if image.label and image.label not in labels:
            raise _refuse("file_view_image_unmatched", f"no mark {image.label} for its crop")
        side = _image_side(image.data)
        if side > MAX_IMAGE_SIDE:
            raise _refuse(
                "file_view_image_too_large",
                f"an image is {side} px on its longest side; at most {MAX_IMAGE_SIDE}",
            )
    return first


def _file_note(app: "FastAPI", file: dict[str, str]) -> str:
    """A note when the file is not in its workspace now (the comment still reaches you)."""
    from clio_agent.gact.artifacts.minting import _workspace_root  # noqa: PLC0415

    root = _workspace_root(app, file["workspace_id"]) if file["workspace_id"] else None
    if root is None:
        return ""
    target = (root / file["path"]).resolve()
    if not target.is_relative_to(root.resolve()) or not target.is_file():
        return f"The file {file['path']} is not in its workspace now (file_view_file_missing)."
    return ""


def render_file_view_comments(
    app: "FastAPI",
    session_id: str,
    attachment: CommentAttachment,
    folder: Path,
    images: bool,
) -> RenderedAttachment:
    """Regions and points on a file view: the browser's overview and crops, and per mark
    the view state when it was made and what the picture could not show."""

    from clio_agent.gact.mcp_app_look import coverage_lines  # noqa: PLC0415

    file = validate_file_view_attachment(attachment)
    labels = [mark.label for mark in attachment.marks]
    pictures, numbers = attachment_images(attachment, folder, labels)
    lines = [
        f"Comment on the file view “{file['name']}” (a {file['kind']} of the workspace file "
        f"{file['path']}, drawn in the person's browser as they see it)."
    ]
    note = _file_note(app, file)
    if note:
        lines.append(note)
    if pictures:
        if not images:
            lines.append(
                "Images were not sent: the selected model does not accept images. The overview "
                f"and crops are saved in {folder}."
            )
        elif not pictures[0][0].startswith("S"):
            lines.append(f"Image 1 is {pictures[0][0]}.")
    shared: list[str] | None = None
    blocks: list[str] = []
    for mark in attachment.marks:
        anchor = mark.anchor
        source = anchor.source
        resolved = ResolvedMark(mode="region")
        if anchor.rect:
            resolved.lines.append(("Box", view_box(anchor)))
        point = source.get("point")
        if isinstance(point, list) and len(point) == 2:
            resolved.lines.append(
                ("Point", f"x {round(point[0] * 100)}%, y {round(point[1] * 100)}% of the view")
            )
        state = state_lines(source.get("view_state"))
        if state and state != shared:
            resolved.lines.append(("View", "\n".join(["state when the mark was made:", *state])))
            shared = state
        elif state:
            resolved.lines.append(("View", "as for the mark before"))
        else:
            resolved.notes.append("The view reported no state (file_view_state_missing).")
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
            resolved.notes.extend(
                line
                for line in coverage_lines({"skipped": skipped})
                if line.startswith("Not in the image")
            )
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
