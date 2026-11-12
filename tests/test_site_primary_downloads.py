"""Every download the release site offers must be a build that runs standalone.

A "lite" desktop build is attach-only: it ships no CLIO backend, so a user who
installs it from the site lands on a Deploy dialog that can never finish (#1412,
#875). The release matrix builds bundled installers only for some formats (Linux
has bundled ``.deb``/``.rpm`` but no bundled AppImage), so every download the site
offers must be a format the release actually ships bundled. The bundled formats come
from ``scripts/check_release_completeness.py`` (the release gate's own list), never a
second hand-typed copy.

The site's platform table lives in ``site/src/lib/downloads.ts`` (``PLATFORMS``).
Windows and macOS have one ``primary`` download; Linux has none and lists its
``distributions`` instead, each with its package (#2168).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from scripts.check_release_completeness import EXPECTED_ASSETS

ROOT = Path(__file__).parents[1]
DOWNLOADS_TS = ROOT / "site" / "src" / "lib" / "downloads.ts"

# Release-gate entries for updater payloads, not installers a user downloads.
NOT_INSTALLERS = ("updater", "sig", "Apple archive")


def _bundled_extensions() -> set[str]:
    """Lower-case file extensions the release gate requires a bundled installer for."""
    extensions: set[str] = set()
    for label, pattern in EXPECTED_ASSETS:
        if not label.startswith("bundled") or any(word in label for word in NOT_INSTALLERS):
            continue
        match = re.search(r"-bundled((?:\\\.[A-Za-z]+)+)\$$", pattern)
        if match:
            extensions.add(match.group(1).replace("\\.", ".").lower())
    return extensions


def _format_extensions() -> dict[str, str]:
    """Parse ``EXTENSIONS`` (format -> file extension) out of downloads.ts."""
    source = DOWNLOADS_TS.read_text(encoding="utf-8")
    block = source[
        source.index("const EXTENSIONS") : source.index("};", source.index("const EXTENSIONS"))
    ]
    return dict(re.findall(r"(\w+):\s*'(\.[a-z][a-z.]*)'", block))


def _offered_formats() -> dict[str, list[str]]:
    """Each platform's downloads in ``PLATFORMS``: its primary and its distributions' packages."""
    source = DOWNLOADS_TS.read_text(encoding="utf-8")
    block = source[source.index("export const PLATFORMS") :]
    offered: dict[str, list[str]] = {}
    for platform in re.split(r"\bos: '", block)[1:]:
        name = platform[: platform.index("'")]
        offered[name] = re.findall(r"(?:primary|choice): \{ format: '(\w+)'", platform)
    return offered


def test_release_gate_lists_bundled_formats() -> None:
    """Guard the parser: the release gate must still name bundled formats."""
    assert _bundled_extensions() == {".msi", ".exe", ".dmg", ".deb", ".rpm", ".pkg.tar.zst"}


def test_site_table_parses() -> None:
    """Guard the parser: every platform offers a download, each with a known extension."""
    offered = _offered_formats()
    assert set(offered) == {"windows", "macos", "linux"}
    assert all(offered.values()), offered
    assert {fmt for formats in offered.values() for fmt in formats} <= set(_format_extensions())
    assert {"deb", "rpm", "pacman"} <= set(offered["linux"])


@pytest.mark.parametrize("platform", ["windows", "macos", "linux"])
def test_every_download_is_a_bundled_format(platform: str) -> None:
    """Each download a platform offers resolves to a format the release ships bundled."""
    bundled = _bundled_extensions()
    for fmt in _offered_formats()[platform]:
        extension = _format_extensions()[fmt]
        assert extension in bundled, (
            f"{platform} download {fmt!r} ({extension}) has no bundled build; "
            "it would hand users the attach-only lite installer"
        )
