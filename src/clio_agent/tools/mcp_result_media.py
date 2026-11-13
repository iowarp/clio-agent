"""An MCP tool result's images reach the model as images.

The model-facing text of an MCP result names each image block with a placeholder
(``[image image/png 1280x800]``, :mod:`clio_agent.tools.mcp_result_text`): the bytes
never ride the text lane. Before this module that placeholder was all the model got,
so a server whose tool returns a rendering (a ParaView ``capture``, a plot) could not
be looked at. Each image block of a model-facing call is now kept as a workspace
snapshot and joins the call's media slot (:data:`clio_agent.gact.model_media.CALL_MEDIA`,
or a foreground script's slot), the same path ``view_image`` uses: the agent loop
attaches the pixels after the result's text on the next step, and history re-reads the
snapshot, never the server.

Nothing is attached outside a model step (an app's own call, a host), and an image that
cannot be shown is logged with a typed reason; the text placeholder stays either way.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import logging
from typing import Any

logger = logging.getLogger(__name__)

__all__ = ["show_to_model"]

_SUFFIX = {"image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif", "image/webp": ".webp"}


def _image_blocks(raw_result: Any) -> list[tuple[str, str]]:
    blocks = getattr(raw_result, "content", None)
    if blocks is None and isinstance(raw_result, dict):
        blocks = raw_result.get("content")
    found: list[tuple[str, str]] = []
    for block in blocks or []:
        get = block.get if isinstance(block, dict) else lambda k, b=block: getattr(b, k, None)
        if get("type") != "image":
            continue
        found.append((str(get("mimeType") or get("mime_type") or ""), str(get("data") or "")))
    return found


def show_to_model(raw_result: Any, tool: str) -> int:
    """Attach ``raw_result``'s images to the current model call; returns how many."""
    blocks = _image_blocks(raw_result)
    if not blocks:
        return 0
    from clio_agent.gact import viewed_media  # noqa: PLC0415
    from clio_agent.gact.model_media import (  # noqa: PLC0415
        CALL_MEDIA,
        SCRIPT_MEDIA,
        script_media_max_items,
    )
    from clio_agent.gact.view_image_tool import (  # noqa: PLC0415
        VIEW_IMAGE_DESCRIPTOR_TYPE,
        VIEW_IMAGE_MEDIA_TYPES,
    )
    from clio_agent.providers.native_attachment_bounds import check_block_bytes  # noqa: PLC0415

    script = SCRIPT_MEDIA.get()
    slot = script if script is not None else CALL_MEDIA.get()
    if not isinstance(slot, list):
        if slot is not None:  # a background script: no model step will see it
            logger.warning("mcp result image not shown reason=no_model_step tool=%s", tool)
        return 0
    shown = 0
    for index, (media_type, encoded) in enumerate(blocks, start=1):
        label = f"image {index} returned by {tool}"
        try:
            if media_type not in VIEW_IMAGE_MEDIA_TYPES:
                raise ValueError(f"unsupported media type {media_type!r}")
            if script is not None and len(slot) >= script_media_max_items():
                raise ValueError("the script's media limit is reached")
            data = base64.b64decode(encoded, validate=True)
            check_block_bytes("image", len(data), label=label)
            snapshot, snapshot_sha256 = viewed_media.snapshot(data, _SUFFIX[media_type])
        except (ValueError, binascii.Error, OSError) as exc:
            logger.warning(
                "mcp result image not shown reason=%s tool=%s detail=%s",
                type(exc).__name__,
                tool,
                exc,
            )
            continue
        slot.append(
            {
                "type": VIEW_IMAGE_DESCRIPTOR_TYPE,
                "path": snapshot,
                "label": f"[{label}]",
                "media_type": media_type,
                "size_bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
                "snapshot": snapshot,
                "snapshot_sha256": snapshot_sha256,
                "attached_to_model": True,
                "delivery": "tool_result",
            }
        )
        shown += 1
    return shown
