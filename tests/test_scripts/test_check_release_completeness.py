"""Tests for the release-completeness check (iowarp/clio-agent#841).

Proves the check flags a silently-incomplete release (the v0.5.17 case: the
``aarch64`` bundled ``.dmg`` never uploaded) and passes on a complete asset
listing, using the real ``EXPECTED_ASSETS`` matrix against fixture name lists.
"""

from __future__ import annotations

import re
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

# The installer scripts + `clio` launchers the installers job uploads. The
# check became the PUBLISH GATE (the release stays a draft until it passes),
# so everything `releases/latest` consumers fetch is now expected. The
# v0.5.17 fixture above had this "installer noise" stripped, so it is kept
# separate too.
_INSTALLER_ASSETS: list[str] = [
    "install.sh",
    "install.ps1",
    "desktop.sh",
    "desktop.ps1",
    "uninstall.sh",
    "uninstall.ps1",
    "clio",
    "clio.cmd",
    "clio.ps1",
]

_INSTALLER_LABELS: set[str] = {
    "installer script (POSIX)",
    "installer script (PowerShell)",
    "desktop installer (macOS)",
    "desktop installer (Windows)",
    "uninstaller script (POSIX)",
    "uninstaller script (PowerShell)",
    "clio launcher (POSIX)",
    "clio launcher (cmd)",
    "clio launcher (PowerShell)",
}

# The complete listing = v0.5.17 plus the one asset it dropped, plus the
# signing-era and installer assets its fixture lacks.
_COMPLETE_ASSETS: list[str] = [
    *_V0517_ASSETS,
    "CLIO.Desktop_0.7.1_aarch64-bundled.dmg",
    *_SIGNING_ASSETS,
    *_INSTALLER_ASSETS,
]

# The real, complete v0.9.4.18 release listing (`gh release view v0.9.4.18
# --json assets`), captured 2026-09-27: the last release published under the
# old side-effect-creation pipeline. The publish gate must pass on it -- a
# gate that rejects a known-good release would strand every future release
# as a draft.
_V09418_ASSETS: list[str] = """
clio clio-agent-aarch64-pc-windows-msvc.exe clio-desktop-aarch64-pc-windows-msvc.exe
clio-tui-darwin-amd64 clio-tui-darwin-amd64.sha256 clio-tui-darwin-arm64
clio-tui-darwin-arm64.sha256 clio-tui-linux-amd64 clio-tui-linux-amd64.sha256
clio-tui-linux-arm64 clio-tui-linux-arm64.sha256 clio-tui-windows-amd64.exe
clio-tui-windows-amd64.exe.sha256 clio-tui-windows-arm64.exe
clio-tui-windows-arm64.exe.sha256 clio-web-0.9.4.18.zip clio.cmd
CLIO.Desktop-0.9.4.18-1.aarch64-bundled.rpm CLIO.Desktop-0.9.4.18-1.aarch64.rpm
CLIO.Desktop-0.9.4.18-1.x86_64-bundled.rpm CLIO.Desktop-0.9.4.18-1.x86_64.rpm
CLIO.Desktop-aarch64-apple-darwin-bundled.app.tar.gz
CLIO.Desktop-aarch64-apple-darwin-bundled.app.tar.gz.sig
CLIO.Desktop-aarch64-apple-darwin.app.tar.gz
CLIO.Desktop-aarch64-apple-darwin.app.tar.gz.sig
CLIO.Desktop-x86_64-apple-darwin.app.tar.gz
CLIO.Desktop-x86_64-apple-darwin.app.tar.gz.sig
CLIO.Desktop_0.9.4.18_aarch64-bundled.dmg CLIO.Desktop_0.9.4.18_aarch64.AppImage
CLIO.Desktop_0.9.4.18_aarch64.AppImage.sig CLIO.Desktop_0.9.4.18_aarch64.dmg
CLIO.Desktop_0.9.4.18_amd64-bundled.deb CLIO.Desktop_0.9.4.18_amd64.AppImage
CLIO.Desktop_0.9.4.18_amd64.AppImage.sig CLIO.Desktop_0.9.4.18_amd64.deb
CLIO.Desktop_0.9.4.18_arm64-bundled.deb CLIO.Desktop_0.9.4.18_arm64.deb
CLIO.Desktop_0.9.4.18_x64-setup-bundled.exe CLIO.Desktop_0.9.4.18_x64-setup-bundled.exe.sig
CLIO.Desktop_0.9.4.18_x64-setup.exe CLIO.Desktop_0.9.4.18_x64-setup.exe.sig
CLIO.Desktop_0.9.4.18_x64.dmg CLIO.Desktop_0.9.4.18_x64_en-US-bundled.msi
CLIO.Desktop_0.9.4.18_x64_en-US-bundled.msi.sig CLIO.Desktop_0.9.4.18_x64_en-US.msi
CLIO.Desktop_0.9.4.18_x64_en-US.msi.sig clio.ps1 install.ps1 install.sh latest-lite.json
latest.json SHA256SUMS.aarch64-apple-darwin.bundled.txt
SHA256SUMS.aarch64-apple-darwin.lite.txt SHA256SUMS.aarch64-pc-windows-msvc.lite.txt
SHA256SUMS.aarch64-unknown-linux-gnu.bundled.txt SHA256SUMS.aarch64-unknown-linux-gnu.lite.txt
SHA256SUMS.web.txt SHA256SUMS.x86_64-apple-darwin.lite.txt
SHA256SUMS.x86_64-pc-windows-msvc.bundled.txt SHA256SUMS.x86_64-pc-windows-msvc.lite.txt
SHA256SUMS.x86_64-unknown-linux-gnu.bundled.txt SHA256SUMS.x86_64-unknown-linux-gnu.lite.txt
uninstall.ps1 uninstall.sh
""".split()


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
    assert labels == {"bundled dmg (aarch64 macOS)"} | _SIGNING_LABELS | _INSTALLER_LABELS


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
    """Unexpected extras (checksums, the lite ARM sidecar) never cause a failure."""
    noisy = [
        *_COMPLETE_ASSETS,
        "SHA256SUMS.web.txt",
        "clio-tui-linux-amd64.sha256",
        "clio-agent-aarch64-pc-windows-msvc.exe",
    ]
    assert find_missing(noisy) == []


def test_real_v09418_release_passes_the_publish_gate() -> None:
    """The historical release predates only the new desktop terminal installers."""
    assert {label for label, _ in find_missing(_V09418_ASSETS)} == {
        "desktop installer (macOS)",
        "desktop installer (Windows)",
    }
    assert find_missing([*_V09418_ASSETS, "desktop.sh", "desktop.ps1"]) == []


def test_each_installer_asset_is_individually_required() -> None:
    """Dropping any single installer script or launcher names exactly that asset.

    These are fetched through ``releases/latest`` by the scripted install
    pathway, so a release missing one must stay a draft.
    """
    for dropped in _INSTALLER_ASSETS:
        listing = [
            name for name in [*_V09418_ASSETS, "desktop.sh", "desktop.ps1"] if name != dropped
        ]
        missing = find_missing(listing)
        assert len(missing) == 1, (dropped, missing)
        label, pattern = missing[0]
        assert label in _INSTALLER_LABELS
        assert re.search(pattern, dropped)


def test_release_without_updater_manifests_fails_the_gate() -> None:
    """The v0.9.4.19 failure shape: every bundle uploaded, manifests not yet generated.

    Publishing (and so becoming ``latest``) in this state is exactly what made
    ``releases/latest/download/latest-lite.json`` 404; the gate must name both.
    """
    listing = [
        n
        for n in [*_V09418_ASSETS, "desktop.sh", "desktop.ps1"]
        if n not in {"latest.json", "latest-lite.json"}
    ]
    labels = {label for label, _ in find_missing(listing)}
    assert labels == {"Tauri update manifest (bundled)", "Tauri update manifest (lite)"}


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
