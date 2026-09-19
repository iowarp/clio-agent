#!/usr/bin/env python3
"""Render the CLIO Windows installer art (NSIS header + sidebar bitmaps).

NSIS takes the two wizard images as bitmaps with hard format requirements:
uncompressed 24-bit BMP, ``header.bmp`` exactly 150x57 and ``sidebar.bmp``
exactly 164x314. A wrong-sized or compressed bitmap does not fail the Tauri
build -- it only ever shows up as a broken page in a shipped installer -- so
the committed artefacts are generated here from the tracked brand assets
instead of being hand-cut, and validated by gact-tui's
``desktop/scripts/check-installer-art.mjs``.

The bitmaps are committed under ``branding/clio/installer/``; this script is the
record of how they were made and the way to regenerate them after a brand
change. Run it with::

    uv run scripts/gen_installer_art.py

Replacing the art by hand is equally valid -- drop in two BMPs that satisfy the
checker and this script becomes provenance for the previous pair.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MARK = REPO_ROOT / "branding" / "logo_cropeed.png"
DEFAULT_OUT_DIR = REPO_ROOT / "branding" / "clio" / "installer"

HEADER_SIZE = (150, 57)
SIDEBAR_SIZE = (164, 314)

# Brand palette. The accent is branding/clio/brand.json's "accent"; the rest are
# the neutral ink/panel tones the mark is designed to sit on.
ACCENT = (234, 123, 42)
HEADER_BG = (255, 255, 255)  # matches MUI_BGCOLOR, so the band reads as one surface
HEADER_INK = (26, 42, 48)
SIDEBAR_TOP = (17, 36, 43)
SIDEBAR_BOTTOM = (8, 17, 21)
SIDEBAR_INK = (255, 255, 255)
SIDEBAR_MUTED = (138, 160, 168)

# Supersampling factor. Text and the mark are drawn at this scale and reduced
# with LANCZOS, which is what keeps 11px type legible at these bitmap sizes.
SCALE = 4

BOLD_FONTS = ("segoeuib.ttf", "seguisb.ttf", "DejaVuSans-Bold.ttf")
REGULAR_FONTS = ("segoeui.ttf", "DejaVuSans.ttf")


def _load_font(candidates: tuple[str, ...], size: int) -> ImageFont.FreeTypeFont:
    """Return the first available font among ``candidates`` at ``size``.

    Segoe UI is the Windows shell face the installer sits in front of; DejaVu
    ships with matplotlib (a declared dependency) so the script still renders on
    a Linux runner or a clean checkout.

    Args:
        candidates: Font file names, most preferred first.
        size: Point size to load.

    Returns:
        The loaded font.

    Raises:
        RuntimeError: If none of the candidates could be loaded.
    """
    for name in candidates:
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    raise RuntimeError(f"none of these fonts could be loaded: {', '.join(candidates)}")


def _trimmed_mark(mark_path: Path) -> Image.Image:
    """Load the CLIO mark and crop it to its opaque bounding box.

    The tracked logo carries a wide transparent margin, which would shrink the
    mark to nothing once scaled into a 57px-tall band.

    Args:
        mark_path: Path to the RGBA logo.

    Returns:
        The cropped RGBA mark.
    """
    mark = Image.open(mark_path).convert("RGBA")
    box = mark.getbbox()
    if box is None:
        raise ValueError(f"{mark_path} is fully transparent")
    return mark.crop(box)


def _fit_height(mark: Image.Image, height: int) -> Image.Image:
    """Scale ``mark`` to exactly ``height`` pixels tall, preserving aspect."""
    width = max(1, round(mark.width * height / mark.height))
    return mark.resize((width, height), Image.Resampling.LANCZOS)


def _flatten(canvas: Image.Image) -> Image.Image:
    """Reduce a supersampled RGBA canvas to its final RGB size."""
    target = (canvas.width // SCALE, canvas.height // SCALE)
    return canvas.resize(target, Image.Resampling.LANCZOS).convert("RGB")


def render_header(mark_path: Path = DEFAULT_MARK) -> Image.Image:
    """Render the 150x57 NSIS header band: mark, wordmark, accent rule.

    Args:
        mark_path: Path to the RGBA logo.

    Returns:
        A 150x57 RGB image.
    """
    width, height = (HEADER_SIZE[0] * SCALE, HEADER_SIZE[1] * SCALE)
    canvas = Image.new("RGBA", (width, height), (*HEADER_BG, 255))

    mark = _fit_height(_trimmed_mark(mark_path), 37 * SCALE)
    mark_x, mark_y = 10 * SCALE, (height - mark.height) // 2
    canvas.alpha_composite(mark, (mark_x, mark_y))

    draw = ImageDraw.Draw(canvas)
    text_x = mark_x + mark.width + 9 * SCALE
    wordmark = _load_font(BOLD_FONTS, 21 * SCALE)
    # Anchor "ls": left edge, baseline of the line box, so the optical centre of
    # the caps sits on the mark's centre rather than drifting with the ascent.
    draw.text(
        (text_x, height // 2 + 7 * SCALE),
        "CLIO",
        font=wordmark,
        fill=(*HEADER_INK, 255),
        anchor="ls",
    )
    draw.rectangle(
        [
            text_x,
            height // 2 + 12 * SCALE,
            text_x + 30 * SCALE,
            height // 2 + 13 * SCALE + SCALE // 2,
        ],
        fill=(*ACCENT, 255),
    )
    return _flatten(canvas)


def render_sidebar(mark_path: Path = DEFAULT_MARK) -> Image.Image:
    """Render the 164x314 NSIS welcome/finish panel.

    A dark panel so the teal mark and orange core carry the page; the wordmark
    and the Gnosis Research Center attribution sit under an accent rule.

    Args:
        mark_path: Path to the RGBA logo.

    Returns:
        A 164x314 RGB image.
    """
    width, height = (SIDEBAR_SIZE[0] * SCALE, SIDEBAR_SIZE[1] * SCALE)
    canvas = Image.new("RGBA", (width, height), (*SIDEBAR_TOP, 255))

    gradient = ImageDraw.Draw(canvas)
    for row in range(height):
        blend = row / (height - 1)
        gradient.line(
            [(0, row), (width, row)],
            fill=(
                round(SIDEBAR_TOP[0] + (SIDEBAR_BOTTOM[0] - SIDEBAR_TOP[0]) * blend),
                round(SIDEBAR_TOP[1] + (SIDEBAR_BOTTOM[1] - SIDEBAR_TOP[1]) * blend),
                round(SIDEBAR_TOP[2] + (SIDEBAR_BOTTOM[2] - SIDEBAR_TOP[2]) * blend),
                255,
            ),
        )

    mark = _fit_height(_trimmed_mark(mark_path), 84 * SCALE)
    canvas.alpha_composite(mark, ((width - mark.width) // 2, 62 * SCALE))

    draw = ImageDraw.Draw(canvas)
    centre = width // 2
    draw.text(
        (centre, 183 * SCALE),
        "CLIO",
        font=_load_font(BOLD_FONTS, 30 * SCALE),
        fill=(*SIDEBAR_INK, 255),
        anchor="ms",
    )
    draw.rectangle(
        [centre - 19 * SCALE, 195 * SCALE, centre + 19 * SCALE, 195 * SCALE + max(1, SCALE // 2)],
        fill=(*ACCENT, 255),
    )
    tagline = _load_font(REGULAR_FONTS, 11 * SCALE)
    for offset, line in ((213, "by the Gnosis"), (227, "Research Center")):
        draw.text(
            (centre, offset * SCALE), line, font=tagline, fill=(*SIDEBAR_MUTED, 255), anchor="ms"
        )

    # Base rule: a full-width accent edge so the panel ends deliberately rather
    # than fading into the dialog's white body.
    draw.rectangle([0, height - 3 * SCALE, width, height], fill=(*ACCENT, 255))
    return _flatten(canvas)


def write_art(out_dir: Path, mark_path: Path = DEFAULT_MARK) -> list[Path]:
    """Render both bitmaps into ``out_dir`` as uncompressed 24-bit BMPs.

    Args:
        out_dir: Directory to write ``header.bmp`` and ``sidebar.bmp`` into.
        mark_path: Path to the RGBA logo.

    Returns:
        The paths written, in the order header, sidebar.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for name, image in (
        ("header.bmp", render_header(mark_path)),
        ("sidebar.bmp", render_sidebar(mark_path)),
    ):
        path = out_dir / name
        # Pillow writes BI_RGB 24-bit for an RGB image, which is what NSIS needs.
        image.save(path, format="BMP")
        written.append(path)
    return written


def main() -> None:
    """Command-line entry point."""
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--mark",
        type=Path,
        default=DEFAULT_MARK,
        help="RGBA logo to render (default: the tracked CLIO mark)",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=DEFAULT_OUT_DIR,
        help="output directory (default: branding/clio/installer)",
    )
    args = parser.parse_args()

    for path in write_art(args.out_dir, args.mark):
        with Image.open(path) as rendered:
            print(f"wrote {path} ({rendered.width}x{rendered.height})")


if __name__ == "__main__":
    main()
