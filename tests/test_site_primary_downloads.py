"""The release site's primary download for each OS must be a build that runs standalone.

A "lite" desktop build is attach-only: it ships no CLIO backend, so a user who
installs it from the primary button lands on a Deploy dialog that can never
finish (#1412, #875). The release matrix builds bundled installers only for
some formats (Linux has bundled ``.deb``/``.rpm`` but no bundled AppImage), so
the site's primary buttons must point at a format the release actually ships
bundled. The bundled formats come from ``scripts/check_release_completeness.py``
(the release gate's own list), never a second hand-typed copy.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from scripts.check_release_completeness import EXPECTED_ASSETS

SITE_ROOT = Path(__file__).parents[1] / "site"
SITE_INDEX = SITE_ROOT / "index.html"
SITE_APP = SITE_ROOT / "app.js"


def _bundled_extensions() -> set[str]:
    """Lower-case file extensions the release gate requires a bundled installer for."""
    extensions: set[str] = set()
    for label, pattern in EXPECTED_ASSETS:
        if not label.startswith("bundled"):
            continue
        match = re.search(r"-bundled\\\.([A-Za-z]+)\$$", pattern)
        if match:
            extensions.add("." + match.group(1).lower())
    return extensions


def _download_extensions() -> dict[str, list[str]]:
    """Parse ``DL_EXT`` (download key -> candidate extensions) out of ``app.js``."""
    source = SITE_APP.read_text(encoding="utf-8")
    block = source[source.index("var DL_EXT = {") : source.index("};", source.index("var DL_EXT"))]
    return {
        key: re.findall(r'"(\.[a-z]+)"', exts)
        for key, exts in re.findall(r'"([a-z-]+)":\s*\[([^\]]*)\]', block)
    }


def _card_primary_key(platform: str) -> str:
    html = SITE_INDEX.read_text(encoding="utf-8")
    card_start = html.index(f'data-os="{platform}"')
    card_end = html.index('class="os-card"', card_start) if platform != "linux" else len(html)
    card = html[card_start:card_end]
    match = re.search(r'class="btn btn-primary os-primary" data-dl="([a-z-]+)"', card)
    assert match, f"{platform} card has no primary download control"
    return match.group(1)


def _hero_primary_keys() -> dict[str, str]:
    source = SITE_APP.read_text(encoding="utf-8")
    block = source[source.index("var primaryKey = {") : source.index("}[os]")]
    return dict(re.findall(r'(windows|macos|linux):\s*"([a-z-]+)"', block))


def test_release_gate_lists_bundled_formats() -> None:
    """Guard the parser: the release gate must still name bundled formats."""
    assert {".msi", ".exe", ".dmg", ".deb", ".rpm"} <= _bundled_extensions()


@pytest.mark.parametrize("platform", ["windows", "macos", "linux"])
def test_card_primary_download_is_a_bundled_format(platform: str) -> None:
    """Each OS card's primary button resolves to a format the release ships bundled."""
    key = _card_primary_key(platform)
    extensions = _download_extensions()[key]
    assert set(extensions) & _bundled_extensions(), (
        f"{platform} primary download {key!r} ({extensions}) has no bundled build; "
        "it would hand users the attach-only lite installer"
    )


@pytest.mark.parametrize("platform", ["windows", "macos", "linux"])
def test_hero_download_is_a_bundled_format(platform: str) -> None:
    """The hero "Download for <OS>" button follows the same rule as the cards."""
    key = _hero_primary_keys()[platform]
    extensions = _download_extensions()[key]
    assert set(extensions) & _bundled_extensions(), (
        f"hero download for {platform} uses {key!r} ({extensions}), which has no bundled build"
    )


def test_card_note_follows_its_primary_download() -> None:
    """The per-card variant caption describes the card's primary download."""
    html = SITE_INDEX.read_text(encoding="utf-8")
    for platform in ("windows", "macos", "linux"):
        key = _card_primary_key(platform)
        assert f'data-dl-note="{key}"' in html, f"{platform} caption is not tied to {key!r}"
