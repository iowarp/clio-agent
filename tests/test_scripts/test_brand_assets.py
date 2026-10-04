"""Validate vector propagation and readable native icon exports."""

from __future__ import annotations

import json
from pathlib import Path

from PIL import Image

from scripts.gen_brand_assets import BRAND, MARK_COPIES, REPO_ROOT, write_native_icons


def test_shipped_vector_copies_preserve_master_and_full_color_profile() -> None:
    """Website and workspace branding must not silently select an older mark."""
    master = (REPO_ROOT / BRAND / "mark.svg").read_bytes()
    for relative in MARK_COPIES:
        assert (REPO_ROOT / relative).read_bytes() == master, relative
    profile = json.loads((REPO_ROOT / BRAND / "brand.json").read_text())
    assert profile["logoImage"] == profile["faviconSvg"] == "mark.svg"
    assert profile["wordmarkImage"] == "wordmark.svg"
    assert not profile.get("iconSvg"), "A monochrome mask overrides the approved color mark"


def test_native_icon_exports_decode_at_required_sizes(tmp_path: Path) -> None:
    """Both platform formats preserve real artwork and transparent corners."""
    write_native_icons(REPO_ROOT / BRAND / "mark.png", tmp_path)
    with Image.open(tmp_path / "icon.ico") as windows:
        assert windows.format == "ICO"
        assert {(16, 16), (32, 32), (48, 48), (256, 256)} <= windows.ico.sizes()
        windows.load()
        assert windows.convert("RGBA").getpixel((0, 0))[3] == 0
        assert windows.getbbox() is not None
    with Image.open(tmp_path / "icon.icns") as mac:
        assert mac.format == "ICNS"
        assert mac.size == (1024, 1024)
        mac.load()
        assert mac.convert("RGBA").getpixel((0, 0))[3] == 0
        assert mac.getbbox() is not None
