"""Tests for the CLIO Windows installer art (NSIS header + sidebar bitmaps).

Two things are pinned here, because NSIS fails at neither:

* :mod:`scripts.gen_installer_art` renders bitmaps in the exact format NSIS
  needs (uncompressed 24-bit BMP, exact dimensions), and
* the COMMITTED art under ``branding/clio/installer`` is still in that format,
  together with the overlay and workflow wiring that gets it in front of the
  bundler. A hand-replaced bitmap that is 32-bit, RLE-compressed or a few
  pixels off builds green and ships a broken installer page.

The BMP header parsing mirrors gact-tui's ``check-installer-art.mjs``, which is
the same assertion on the CI side of the fence.
"""

from __future__ import annotations

import json
import struct
from pathlib import Path

import pytest

from scripts.gen_installer_art import (
    HEADER_SIZE,
    SIDEBAR_SIZE,
    render_header,
    render_sidebar,
    write_art,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
ART_DIR = REPO_ROOT / "branding" / "clio" / "installer"
OVERLAY = REPO_ROOT / "branding" / "clio" / "tauri.clio.conf.json"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "clio-bundles.yml"

BI_RGB = 0
REQUIRED_BITS_PER_PIXEL = 24

# file name -> (width, height), matching check-installer-art.mjs's REQUIREMENTS
# and Tauri's documented nsis.headerImage / nsis.sidebarImage sizes.
REQUIREMENTS: dict[str, tuple[int, int]] = {
    "header.bmp": HEADER_SIZE,
    "sidebar.bmp": SIDEBAR_SIZE,
}


def _read_bmp_header(path: Path) -> tuple[int, int, int, int]:
    """Parse a BMP's width, height, bit depth and compression from its headers.

    Args:
        path: The bitmap to parse.

    Returns:
        ``(width, height, bits_per_pixel, compression)``; height is positive
        even for a top-down bitmap (a negative height only marks row order).
    """
    raw = path.read_bytes()
    assert len(raw) >= 54, f"{path.name} is too short to be a BMP"
    assert raw[:2] == b"BM", f"{path.name} is missing the BM magic"
    (dib_size,) = struct.unpack_from("<I", raw, 14)
    assert dib_size >= 40, f"{path.name} has a pre-BITMAPINFOHEADER DIB header"
    width, height = struct.unpack_from("<ii", raw, 18)
    (bits_per_pixel,) = struct.unpack_from("<H", raw, 28)
    (compression,) = struct.unpack_from("<I", raw, 30)
    return width, abs(height), bits_per_pixel, compression


@pytest.mark.parametrize("name", sorted(REQUIREMENTS))
def test_committed_art_is_nsis_ready(name: str) -> None:
    """The tracked bitmaps are exactly what NSIS accepts, whoever made them."""
    path = ART_DIR / name
    assert path.is_file(), f"{name} is missing from branding/clio/installer"
    width, height, bits_per_pixel, compression = _read_bmp_header(path)
    assert (width, height) == REQUIREMENTS[name]
    assert bits_per_pixel == REQUIRED_BITS_PER_PIXEL
    assert compression == BI_RGB


def test_render_header_has_the_nsis_header_size() -> None:
    image = render_header()
    assert (image.width, image.height) == HEADER_SIZE
    assert image.mode == "RGB"


def test_render_sidebar_has_the_nsis_sidebar_size() -> None:
    image = render_sidebar()
    assert (image.width, image.height) == SIDEBAR_SIZE
    assert image.mode == "RGB"


def test_write_art_round_trips_to_uncompressed_24_bit(tmp_path: Path) -> None:
    """A fresh render lands on disk in the format the checker demands."""
    written = write_art(tmp_path)
    assert [path.name for path in written] == ["header.bmp", "sidebar.bmp"]
    for path in written:
        width, height, bits_per_pixel, compression = _read_bmp_header(path)
        assert (width, height) == REQUIREMENTS[path.name]
        assert bits_per_pixel == REQUIRED_BITS_PER_PIXEL
        assert compression == BI_RGB


def test_rendered_art_is_not_blank() -> None:
    """Guard the whole point of the art: a mark actually got composited.

    A transparent source or a broken crop would still produce correctly sized
    bitmaps -- they would just be flat fills, which no format check can see.
    """
    for image in (render_header(), render_sidebar()):
        colours = image.convert("RGB").getcolors(maxcolors=image.width * image.height)
        assert colours is not None
        assert len(colours) > 64, "art is nearly flat -- the mark did not render"


def test_overlay_declares_the_art_it_ships() -> None:
    """The CLIO overlay points NSIS at files that exist in the art directory."""
    nsis = json.loads(OVERLAY.read_text(encoding="utf-8"))["bundle"]["windows"]["nsis"]
    assert nsis["headerImage"] == "installer/header.bmp"
    assert nsis["sidebarImage"] == "installer/sidebar.bmp"
    # installerIcon reuses the icon family the workflow already applies.
    assert nsis["installerIcon"] == "icons/icon.ico"
    for relative in (nsis["headerImage"], nsis["sidebarImage"]):
        assert (OVERLAY.parent / relative).is_file(), f"overlay references missing {relative}"


def test_bundles_workflow_checks_and_applies_the_art() -> None:
    """The art is validated and copied where the overlay's paths resolve.

    The overlay's ``installer/...`` paths are relative to the merged config's
    location (``desktop/src-tauri``), so the copy is what makes them resolve;
    without it the build fails on a missing bitmap. Ordering matters too: both
    steps must precede the Tauri build.
    """
    workflow = WORKFLOW.read_text(encoding="utf-8")
    check_at = workflow.find("name: Check CLIO installer art")
    apply_at = workflow.find("name: Apply CLIO installer art")
    build_at = workflow.find("name: Tauri release build")
    assert -1 not in (check_at, apply_at, build_at)
    assert check_at < apply_at < build_at
    assert "check-installer-art.mjs" in workflow
    assert 'dst="external/gact-tui/desktop/src-tauri/installer"' in workflow
    assert 'cp -f "$src"/*.bmp "$dst"/' in workflow
