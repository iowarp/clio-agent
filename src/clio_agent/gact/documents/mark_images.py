"""The images a comment session sends the agent: labelled overviews and per-mark crops.

An overview is the whole page or image with every mark drawn as a box with its label badge,
in the style the A2UI capture's ``labelledPng`` draws (addendum A, section 2). A crop is the
area under one region or element. Both are bounded to ``comments.max_image_side``.
"""

from __future__ import annotations

import io
from collections.abc import Sequence
from dataclasses import dataclass

from PIL import Image, ImageDraw, ImageFont

from clio_agent.gact.documents.comment_settings import max_image_side

__all__ = ["MarkBox", "crop_png", "labelled_overview_png", "png_bytes"]

#: Box and badge colours. A saturated blue with a white keyline reads on light and dark
#: figures alike; the badge text is white on the same blue.
_ACCENT = (37, 99, 235)
_KEYLINE = (255, 255, 255)
_BADGE_TEXT = (255, 255, 255)


@dataclass(frozen=True)
class MarkBox:
    """One mark to draw: its label and its rectangle, normalized to the image."""

    label: str
    rect: tuple[float, float, float, float]


def _bounded(image: Image.Image, limit: int) -> Image.Image:
    longest = max(image.size)
    if longest <= limit:
        return image
    scale = limit / longest
    size = (max(1, round(image.width * scale)), max(1, round(image.height * scale)))
    return image.resize(size, Image.Resampling.LANCZOS)


def png_bytes(image: Image.Image) -> bytes:
    """``image`` as PNG bytes, bounded to the configured longest side."""

    out = io.BytesIO()
    _bounded(image.convert("RGBA") if image.mode == "P" else image, max_image_side()).save(
        out, format="PNG", optimize=True
    )
    return out.getvalue()


def _pixel_box(image: Image.Image, rect: Sequence[float]) -> tuple[int, int, int, int]:
    x, y, width, height = rect
    left = max(0, min(image.width - 1, round(x * image.width)))
    top = max(0, min(image.height - 1, round(y * image.height)))
    right = max(left + 1, min(image.width, round((x + width) * image.width)))
    bottom = max(top + 1, min(image.height, round((y + height) * image.height)))
    return left, top, right, bottom


def labelled_overview_png(image: Image.Image, marks: Sequence[MarkBox]) -> bytes:
    """The whole ``image`` with every mark drawn as a labelled box."""

    canvas = _bounded(image.convert("RGB"), max_image_side()).copy()
    draw = ImageDraw.Draw(canvas)
    unit = max(1.0, max(canvas.size) / 1000)
    line = max(2, round(2 * unit))
    font = ImageFont.load_default(size=max(12, round(14 * unit)))
    for mark in marks:
        left, top, right, bottom = _pixel_box(canvas, mark.rect)
        draw.rectangle((left, top, right, bottom), outline=_KEYLINE, width=line + 2)
        draw.rectangle((left, top, right, bottom), outline=_ACCENT, width=line)
        text_box = draw.textbbox((0, 0), mark.label, font=font)
        pad = round(4 * unit)
        badge_w = text_box[2] - text_box[0] + 2 * pad
        badge_h = text_box[3] - text_box[1] + 2 * pad
        badge_top = top - badge_h if top >= badge_h else top
        draw.rectangle((left, badge_top, left + badge_w, badge_top + badge_h), fill=_ACCENT)
        draw.text(
            (left + pad - text_box[0], badge_top + pad - text_box[1]),
            mark.label,
            fill=_BADGE_TEXT,
            font=font,
        )
    return png_bytes(canvas)


def crop_png(image: Image.Image, rect: Sequence[float]) -> bytes:
    """The area of ``image`` under a normalized ``rect``."""

    return png_bytes(image.crop(_pixel_box(image, rect)))
