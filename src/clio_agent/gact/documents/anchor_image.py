"""Image anchors: a region on a raster image is its crop; an SVG resolves like a page.

A raster image has nothing under a region but pixels, so its entry carries the box and the
crop (addendum A, "What is under each mark"). The crop is cut from the image file at its own
resolution. SVG images take regions and picked elements through the HTML resolver, which
renders the drawing and reads its elements.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from clio_agent.gact.documents.anchor_html import DomResolver
from clio_agent.gact.documents.anchors import (
    AnchorState,
    MarkTarget,
    ResolvedMark,
    register,
)
from clio_agent.gact.documents.mark_images import crop_png
from clio_agent.gact.documents.models import DocumentAnchor

if TYPE_CHECKING:
    from PIL import Image

__all__ = ["ImageResolver", "RasterResolver", "register_builtin"]


def _image(target: MarkTarget) -> "Image.Image | None":
    cached = target.cache.get("raster")
    if cached is None:
        from PIL import Image, UnidentifiedImageError  # noqa: PLC0415 - optional heavy import

        try:
            with Image.open(target.path) as opened:
                opened.seek(0)
                cached = opened.convert("RGB")
        except (OSError, UnidentifiedImageError):
            return None
        target.cache["raster"] = cached
    return cached


class RasterResolver:
    """Regions on PNG, JPEG, WebP and GIF images."""

    def resolve(self, target: MarkTarget, anchor: DocumentAnchor) -> ResolvedMark:
        x, y, width, height = anchor.rect
        mark = ResolvedMark(mode="region", rect=(x, y, width, height))
        mark.lines.append(
            (
                "Box",
                f"x {round(x * 100)}% to {round((x + width) * 100)}%, "
                f"y {round(y * 100)}% to {round((y + height) * 100)}% of the image",
            )
        )
        image = _image(target)
        if image is None:
            mark.notes.append("image_unreadable: the image file could not be decoded")
        else:
            mark.crop = crop_png(image, anchor.rect)
        return mark

    def page_image(self, target: MarkTarget, page_index: int | None) -> "Image.Image | None":
        return _image(target)

    def reanchor(
        self, old: MarkTarget, new: MarkTarget, anchor: DocumentAnchor
    ) -> tuple[AnchorState, DocumentAnchor]:
        # The same rectangle on a changed image is not the same thing: a region stays on its
        # version and is shown beside the same rectangle on the new one (plan, "New versions").
        return "current", anchor


class ImageResolver:
    """The ``image`` profile: SVG through the HTML resolver, everything else as a raster."""

    def __init__(self) -> None:
        self.raster = RasterResolver()
        self.svg = DomResolver(svg=True)

    def _for(self, target: MarkTarget) -> "RasterResolver | DomResolver":
        return self.svg if target.name.lower().endswith(".svg") else self.raster

    def resolve(self, target: MarkTarget, anchor: DocumentAnchor) -> ResolvedMark:
        return self._for(target).resolve(target, anchor)

    def page_image(self, target: MarkTarget, page_index: int | None) -> "Image.Image | None":
        return self._for(target).page_image(target, page_index)

    def reanchor(
        self, old: MarkTarget, new: MarkTarget, anchor: DocumentAnchor
    ) -> tuple[AnchorState, DocumentAnchor]:
        return self._for(new).reanchor(old, new, anchor)


def register_builtin() -> None:
    """Regions on images; picked elements on SVG images."""

    register(("image",), ("region", "dom"), ImageResolver())
