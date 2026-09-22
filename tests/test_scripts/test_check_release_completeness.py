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

# The signed updater payloads + detached signatures + manifests added for
# v0.9.4.1 auto-update (#A7) -- v0.5.17 predates all of these, so they are
# kept as a separate fixture rather than folded into the "real snapshot"
# _V0517_ASSETS list above.
_SIGNING_ASSETS: list[str] = [
    "CLIO.Desktop_0.7.1_x64-setup-bundled.exe.sig",
    "CLIO.Desktop-aarch64-apple-darwin-bundled.app.tar.gz",
    "CLIO.Desktop-aarch64-apple-darwin-bundled.app.tar.gz.sig",
    "CLIO.Desktop_0.7.1_x64-setup.exe.sig",
    "CLIO.Desktop-aarch64-apple-darwin.app.tar.gz",
    "CLIO.Desktop-aarch64-apple-darwin.app.tar.gz.sig",
    "CLIO.Desktop-x86_64-apple-darwin.app.tar.gz",
    "CLIO.Desktop-x86_64-apple-darwin.app.tar.gz.sig",
    "CLIO.Desktop_0.7.1_amd64.AppImage.sig",
    "CLIO.Desktop_0.7.1_aarch64.AppImage.sig",
    "latest.json",
    "latest-lite.json",
]

# The complete listing = v0.5.17 plus the one asset it dropped, plus the
# signing-era assets it predates.
_COMPLETE_ASSETS: list[str] = [
    *_V0517_ASSETS,
    "CLIO.Desktop_0.7.1_aarch64-bundled.dmg",
    *_SIGNING_ASSETS,
]


# The exact EXPECTED_ASSETS labels the signed-updater feature (v0.9.4.1
# auto-update, #A7) introduced -- must track check_release_completeness.py's
# EXPECTED_ASSETS additions 1:1.
_SIGNING_LABELS: set[str] = {
    "bundled nsis sig (x86_64 Windows)",
    "bundled macOS updater bundle (aarch64)",
    "bundled macOS updater sig (aarch64)",
    "lite nsis sig (x86_64 Windows)",
    "lite macOS updater bundle (aarch64)",
    "lite macOS updater sig (aarch64)",
    "lite macOS updater bundle (x86_64)",
    "lite macOS updater sig (x86_64)",
    "lite AppImage sig (x86_64 Linux)",
    "lite AppImage sig (aarch64 Linux)",
    "Tauri update manifest (bundled)",
    "Tauri update manifest (lite)",
}


def test_signing_assets_fixture_matches_the_signing_labels() -> None:
    """_SIGNING_ASSETS satisfies exactly _SIGNING_LABELS -- the two fixtures stay in sync."""
    missing_labels = {label for label, _ in find_missing(_SIGNING_ASSETS)}
    assert missing_labels & _SIGNING_LABELS == set()


def test_v0517_flags_only_the_missing_bundled_dmg_and_predates_signing() -> None:
    """The v0.5.17 listing is missing the aarch64 bundled .dmg (its own era's gap) plus every
    signed-updater asset the later v0.9.4.1 auto-update slice introduced (v0.5.17 shipped with
    none of those)."""
    missing = find_missing(_V0517_ASSETS)
    labels = {label for label, _ in missing}
    assert labels == {"bundled dmg (aarch64 macOS)"} | _SIGNING_LABELS


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
