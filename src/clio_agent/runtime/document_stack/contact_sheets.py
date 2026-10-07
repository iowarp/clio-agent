"""Bounded, labelled page overviews alongside full-resolution review images."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont, ImageOps


def _label_font() -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for name in ("Arial.ttf", "DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(name, 18)
        except OSError:
            continue
    return ImageFont.load_default()


def make_contact_sheets(images: list[dict[str, Any]], output: Path) -> list[dict[str, Any]]:
    """Create two-column, three-row overviews without replacing source page images.

    Inputs are the page images produced by the pixel-budgeted PDF renderer.
    Each sheet retains original page numbers, including sparse selections.
    Thumbnails serve composition review; fine print needs the original images.
    """
    sheets: list[dict[str, Any]] = []
    gutter, tile_width, max_tile_height, label_height = 16, 480, 720, 32
    font = _label_font()
    for start in range(0, len(images), 6):
        group = images[start : start + 6]
        ratios: list[float] = []
        for item in group:
            with Image.open(item["path"]) as page:
                ratios.append(page.height / page.width)
        tile_height = min(max_tile_height, max(1, round(max(ratios) * tile_width)))
        row_height = tile_height + label_height
        rows = (len(group) + 1) // 2
        canvas = Image.new(
            "RGB",
            (2 * tile_width + 3 * gutter, rows * row_height + (rows + 1) * gutter),
            "#e9edf0",
        )
        draw = ImageDraw.Draw(canvas)
        for index, item in enumerate(group):
            x = gutter + (index % 2) * (tile_width + gutter)
            y = gutter + (index // 2) * (row_height + gutter)
            draw.text((x, y + 4), f"Page {item['page']}", font=font, fill="#202124")
            with Image.open(item["path"]) as page:
                thumb = ImageOps.contain(page.convert("RGB"), (tile_width, tile_height))
                canvas.paste(thumb, (x + (tile_width - thumb.width) // 2, y + label_height))
                thumb.close()
        path = output / f"contact-sheet-{len(sheets) + 1:04d}.png"
        canvas.save(path)
        canvas.close()
        with path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        sheets.append(
            {"path": str(path), "pages": [item["page"] for item in group], "sha256": digest}
        )
    return sheets
