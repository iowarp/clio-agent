#!/usr/bin/env python3
"""Assert a clio-agent GH release carries every expected bundle.

The ``clio-bundles.yml`` desktop matrix fans out lite + bundled installers
across six targets, plus a web zip and the ``clio-tui-*`` binaries. Because
``fail-fast: false`` lets one matrix leg fail while the rest upload, a release
can ship *silently incomplete* -- exactly what happened to ``v0.5.17``, which
was missing its ``aarch64`` **bundled** ``.dmg`` (iowarp/clio-agent#841, F-15).
There was no red signal; the gap was only found by hand-diffing the asset list.

This script turns that gap into a named failure. It reads the release's asset
names (one per line on stdin, or from ``--assets-file``) and checks each
*expected* asset against a version-agnostic pattern (the desktop app carries an
independent version, so exact names are not stable release-to-release). Any
expected asset with no matching name is reported and the check exits non-zero.

Extra assets (checksums, installer scripts, launchers) are ignored: the check
asserts completeness, not an exact set.

Run in CI (``release-check`` job) and locally::

    gh release view vX.Y.Z --json assets -q '.assets[].name' \\
        | uv run python scripts/check_release_completeness.py
    uv run python scripts/check_release_completeness.py --assets-file assets.txt
"""

from __future__ import annotations

import argparse
import re
import sys

# Expected assets as ``(label, regex)`` pairs. Each pattern is matched with
# ``re.search`` against every asset name; an expected asset is satisfied when at
# least one name matches. Patterns are version-agnostic (the desktop app version
# and the release version are substituted by ``.*`` / anchored suffixes) so the
# same list holds across releases. Bundled artifacts carry a ``-bundled`` token
# before the extension (staged at clio-bundles.yml); lite artifacts do not, so
# the lite suffix patterns never match a bundled name.
EXPECTED_ASSETS: list[tuple[str, str]] = [
    # Bundled desktop installers (embed the clio-agent runtime). The matrix
    # excludes bundled x86_64-macOS and Windows-on-ARM (no clio-core wheels).
    ("bundled msi (x86_64 Windows)", r"_x64_en-US-bundled\.msi$"),
    ("bundled nsis exe (x86_64 Windows)", r"_x64-setup-bundled\.exe$"),
    ("bundled dmg (aarch64 macOS)", r"aarch64-bundled\.dmg$"),
    ("bundled deb (x86_64 Linux)", r"_amd64-bundled\.deb$"),
    ("bundled deb (aarch64 Linux)", r"_arm64-bundled\.deb$"),
    ("bundled rpm (x86_64 Linux)", r"\.x86_64-bundled\.rpm$"),
    ("bundled rpm (aarch64 Linux)", r"\.aarch64-bundled\.rpm$"),
    # Lite desktop installers (attach-only; no embedded runtime).
    ("lite msi (x86_64 Windows)", r"_x64_en-US\.msi$"),
    ("lite nsis exe (x86_64 Windows)", r"_x64-setup\.exe$"),
    ("lite exe (aarch64 Windows)", r"^clio-desktop-aarch64-pc-windows-msvc\.exe$"),
    ("lite dmg (aarch64 macOS)", r"_aarch64\.dmg$"),
    ("lite dmg (x86_64 macOS)", r"_x64\.dmg$"),
    ("lite deb (x86_64 Linux)", r"_amd64\.deb$"),
    ("lite deb (aarch64 Linux)", r"_arm64\.deb$"),
    ("lite AppImage (x86_64 Linux)", r"_amd64\.AppImage$"),
    ("lite AppImage (aarch64 Linux)", r"_aarch64\.AppImage$"),
    ("lite rpm (x86_64 Linux)", r"\.x86_64\.rpm$"),
    ("lite rpm (aarch64 Linux)", r"\.aarch64\.rpm$"),
    # CLIO TUI binaries (one per OS/arch; installer scripts fetch these).
    ("tui darwin-amd64", r"^clio-tui-darwin-amd64$"),
    ("tui darwin-arm64", r"^clio-tui-darwin-arm64$"),
    ("tui linux-amd64", r"^clio-tui-linux-amd64$"),
    ("tui linux-arm64", r"^clio-tui-linux-arm64$"),
    ("tui windows-amd64", r"^clio-tui-windows-amd64\.exe$"),
    ("tui windows-arm64", r"^clio-tui-windows-arm64\.exe$"),
    # Web SPA bundle (version-stamped zip).
    ("web bundle zip", r"^clio-web-.*\.zip$"),
]


def find_missing(
    asset_names: list[str],
    expected: list[tuple[str, str]] = EXPECTED_ASSETS,
) -> list[tuple[str, str]]:
    """Return the ``(label, pattern)`` pairs with no matching asset name.

    An expected asset is satisfied when at least one entry of ``asset_names``
    matches its pattern (``re.search``). The returned list preserves the order
    of ``expected`` and is empty when the release is complete.
    """
    missing: list[tuple[str, str]] = []
    for label, pattern in expected:
        compiled = re.compile(pattern)
        if not any(compiled.search(name) for name in asset_names):
            missing.append((label, pattern))
    return missing


def _read_asset_names(assets_file: str | None) -> list[str]:
    """Read asset names (one per line) from ``assets_file`` or stdin."""
    if assets_file is not None:
        with open(assets_file, encoding="utf-8") as handle:
            raw = handle.read()
    else:
        raw = sys.stdin.read()
    return [line.strip() for line in raw.splitlines() if line.strip()]


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Return 0 if the release is complete, 1 otherwise."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--assets-file",
        metavar="PATH",
        default=None,
        help="File with asset names (one per line). Defaults to stdin.",
    )
    args = parser.parse_args(argv)

    asset_names = _read_asset_names(args.assets_file)
    if not asset_names:
        print("FAIL: no asset names provided (empty release listing?).")
        return 1

    missing = find_missing(asset_names)
    if not missing:
        print(f"OK: all {len(EXPECTED_ASSETS)} expected release assets present.")
        return 0

    print(f"FAIL: {len(missing)} expected release asset(s) missing (#841):")
    for label, pattern in missing:
        print(f"  {label}  (no name matched /{pattern}/)")
    return 1


if __name__ == "__main__":
    sys.exit(main())
