"""The release site's primary download for each OS must be a build that runs standalone.

A "lite" desktop build is attach-only: it ships no CLIO backend, so a user who
installs it from the primary button lands on a Deploy dialog that can never
finish (#1412, #875). The release matrix builds bundled installers only for
some formats (Linux has bundled ``.deb``/``.rpm`` but no bundled AppImage), so
the site's primary buttons must point at a format the release actually ships
bundled. The bundled formats come from ``scripts/check_release_completeness.py``
(the release gate's own list), never a second hand-typed copy.

The site's platform table lives in ``site/src/lib/downloads.ts`` (``PLATFORMS``);
both the hero button and the download cards read their primary format from it.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from scripts.check_release_completeness import EXPECTED_ASSETS

DOWNLOADS_TS = Path(__file__).parents[1] / "site" / "src" / "lib" / "downloads.ts"


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


def _format_extensions() -> dict[str, str]:
    """Parse ``EXTENSIONS`` (format -> file extension) out of downloads.ts."""
    source = DOWNLOADS_TS.read_text(encoding="utf-8")
    block = source[
        source.index("const EXTENSIONS") : source.index("};", source.index("const EXTENSIONS"))
    ]
    return dict(re.findall(r"(\w+):\s*'(\.[a-z]+)'", block))


def _primary_formats() -> dict[str, str]:
    """Parse each platform's primary download format out of ``PLATFORMS``."""
    source = DOWNLOADS_TS.read_text(encoding="utf-8")
    block = source[source.index("export const PLATFORMS") :]
    return dict(re.findall(r"os: '(\w+)',.*?primary: \{ format: '(\w+)'", block, flags=re.DOTALL))


def test_release_gate_lists_bundled_formats() -> None:
    """Guard the parser: the release gate must still name bundled formats."""
    assert {".msi", ".exe", ".dmg", ".deb", ".rpm"} <= _bundled_extensions()


def test_site_table_parses() -> None:
    """Guard the parser: every platform and its primary format are found."""
    assert set(_primary_formats()) == {"windows", "macos", "linux"}
    assert set(_primary_formats().values()) <= set(_format_extensions())


@pytest.mark.parametrize("platform", ["windows", "macos", "linux"])
def test_primary_download_is_a_bundled_format(platform: str) -> None:
    """Each platform's primary download resolves to a format the release ships bundled."""
    fmt = _primary_formats()[platform]
    extension = _format_extensions()[fmt]
    assert extension in _bundled_extensions(), (
        f"{platform} primary download {fmt!r} ({extension}) has no bundled build; "
        "it would hand users the attach-only lite installer"
    )
