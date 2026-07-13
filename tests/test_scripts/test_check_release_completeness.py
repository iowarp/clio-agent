"""Tests for the release-completeness check (iowarp/clio-agent#841).

Proves the check flags a silently-incomplete release (the v0.5.17 case: the
``aarch64`` bundled ``.dmg`` never uploaded) and passes on a complete asset
listing, using the real ``EXPECTED_ASSETS`` matrix against fixture name lists.
"""

from __future__ import annotations

from pathlib import Path

from scripts.check_release_completeness import EXPECTED_ASSETS, find_missing, main

# The actual v0.5.17 GH release asset names (desktop app version 0.7.1), minus
# the checksum/installer noise the check intentionally ignores. This release
# shipped WITHOUT the aarch64 bundled .dmg -- the gap this check exists to name.
_V0517_ASSETS: list[str] = [
    "clio-desktop-aarch64-pc-windows-msvc.exe",
    "clio-tui-darwin-amd64",
    "clio-tui-darwin-arm64",
    "clio-tui-linux-amd64",
    "clio-tui-linux-arm64",
    "clio-tui-windows-amd64.exe",
    "clio-tui-windows-arm64.exe",
    "clio-web-0.5.17.zip",
    "CLIO.Desktop-0.7.1-1.aarch64-bundled.rpm",
    "CLIO.Desktop-0.7.1-1.aarch64.rpm",
    "CLIO.Desktop-0.7.1-1.x86_64-bundled.rpm",
    "CLIO.Desktop-0.7.1-1.x86_64.rpm",
    "CLIO.Desktop_0.7.1_aarch64.AppImage",
    "CLIO.Desktop_0.7.1_aarch64.dmg",
    "CLIO.Desktop_0.7.1_amd64-bundled.deb",
    "CLIO.Desktop_0.7.1_amd64.AppImage",
    "CLIO.Desktop_0.7.1_amd64.deb",
    "CLIO.Desktop_0.7.1_arm64-bundled.deb",
    "CLIO.Desktop_0.7.1_arm64.deb",
    "CLIO.Desktop_0.7.1_x64-setup-bundled.exe",
    "CLIO.Desktop_0.7.1_x64-setup.exe",
    "CLIO.Desktop_0.7.1_x64.dmg",
    "CLIO.Desktop_0.7.1_x64_en-US-bundled.msi",
    "CLIO.Desktop_0.7.1_x64_en-US.msi",
]

# The complete listing = v0.5.17 plus the one asset it dropped.
_COMPLETE_ASSETS: list[str] = [*_V0517_ASSETS, "CLIO.Desktop_0.7.1_aarch64-bundled.dmg"]


def test_v0517_flags_only_the_missing_bundled_dmg() -> None:
    """The v0.5.17 listing is missing exactly the aarch64 bundled .dmg."""
    missing = find_missing(_V0517_ASSETS)
    labels = [label for label, _ in missing]
    assert labels == ["bundled dmg (aarch64 macOS)"]


def test_complete_listing_has_no_gaps() -> None:
    """A listing with every expected bundle passes."""
    assert find_missing(_COMPLETE_ASSETS) == []


def test_lite_pattern_does_not_satisfy_a_bundled_expectation() -> None:
    """A bundled expectation is not satisfied by the lite artifact alone.

    The lite ``_x64_en-US.msi`` must not mask a missing bundled MSI -- the
    ``-bundled`` token is what distinguishes them.
    """
    lite_only = [n for n in _V0517_ASSETS if "-bundled" not in n]
    missing_labels = {label for label, _ in find_missing(lite_only)}
    assert "bundled msi (x86_64 Windows)" in missing_labels
    assert "lite msi (x86_64 Windows)" not in missing_labels


def test_empty_listing_reports_everything_missing() -> None:
    """An empty release listing flags the whole expected matrix."""
    assert len(find_missing([])) == len(EXPECTED_ASSETS)


def test_extra_assets_are_ignored() -> None:
    """Unexpected extras (checksums, launchers) never cause a failure."""
    noisy = [*_COMPLETE_ASSETS, "SHA256SUMS.web.txt", "install.sh", "clio.cmd"]
    assert find_missing(noisy) == []


def test_main_exits_nonzero_on_incomplete_release(tmp_path: Path) -> None:
    """The CLI returns 1 and names the gap when reading an incomplete file."""
    assets_file = tmp_path / "assets.txt"
    assets_file.write_text("\n".join(_V0517_ASSETS), encoding="utf-8")
    assert main(["--assets-file", str(assets_file)]) == 1


def test_main_exits_zero_on_complete_release(tmp_path: Path) -> None:
    """The CLI returns 0 for a complete release listing."""
    assets_file = tmp_path / "assets.txt"
    assets_file.write_text("\n".join(_COMPLETE_ASSETS), encoding="utf-8")
    assert main(["--assets-file", str(assets_file)]) == 0


def test_main_exits_nonzero_on_empty_input(tmp_path: Path) -> None:
    """An empty file is a failure (not a vacuous pass)."""
    assets_file = tmp_path / "assets.txt"
    assets_file.write_text("", encoding="utf-8")
    assert main(["--assets-file", str(assets_file)]) == 1
