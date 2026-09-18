"""Tests for the signed Tauri updater manifest generator (v0.9.4.1 auto-update).

Exercises :mod:`scripts.gen_tauri_update_manifest` fully offline -- a fixture
asset listing (the same shape ``gh release view --json assets`` prints) via
``--assets-json``, and ``.sig`` contents read from a local ``--sig-dir``
instead of downloaded -- against the REAL staged-name conventions
``clio-bundles.yml``'s "Stage artifacts" step produces, per
iowarp/clio-agent's release-plumbing slice A7.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from scripts.gen_tauri_update_manifest import (
    PLATFORM_PATTERNS,
    build_manifest,
    encode_version,
    main,
)

TAG = "v0.9.4.1"


def _asset(name: str) -> dict[str, str]:
    return {"name": name, "url": f"https://example.invalid/download/{TAG}/{name}"}


def _write_sig(
    sig_dir: Path, installer_name: str, content: str = "untrusted comment: sig\nAB"
) -> None:
    (sig_dir / f"{installer_name}.sig").write_text(content, encoding="utf-8")


# Real staged names (post clio-bundles.yml "Stage artifacts" rename: +N -> .N,
# the -bundled token, and the target-triple suffix on the unversioned macOS
# .app.tar.gz) satisfying every LITE platform in PLATFORM_PATTERNS.
_LITE_INSTALLERS: dict[str, str] = {
    "windows-x86_64": "CLIO.Desktop_0.9.4.1_x64-setup.exe",
    "darwin-aarch64": "CLIO.Desktop-aarch64-apple-darwin.app.tar.gz",
    "darwin-x86_64": "CLIO.Desktop-x86_64-apple-darwin.app.tar.gz",
    "linux-x86_64": "CLIO.Desktop_0.9.4.1_amd64.AppImage",
    "linux-aarch64": "CLIO.Desktop_0.9.4.1_aarch64.AppImage",
}

# Same platforms, BUNDLED variant naming.
_BUNDLED_INSTALLERS: dict[str, str] = {
    "windows-x86_64": "CLIO.Desktop_0.9.4.1_x64-setup-bundled.exe",
    "darwin-aarch64": "CLIO.Desktop-aarch64-apple-darwin-bundled.app.tar.gz",
}


def _assets_for(installers: dict[str, str]) -> list[dict[str, str]]:
    assets: list[dict[str, str]] = []
    for name in installers.values():
        assets.append(_asset(name))
        assets.append(_asset(f"{name}.sig"))
    return assets


def _sig_dir_for(tmp_path: Path, installers: dict[str, str]) -> Path:
    sig_dir = tmp_path / "sigs"
    sig_dir.mkdir()
    for name in installers.values():
        _write_sig(sig_dir, name)
    return sig_dir


def test_manifest_version_encodes_fourth_part_as_build_metadata(tmp_path: Path) -> None:
    """A four-part CLIO maintenance tag encodes its N as numeric SemVer build metadata."""

    assert encode_version("v0.9.4.1") == "0.9.4+1"
    assert encode_version("v0.9.4") == "0.9.4"
    with pytest.raises(ValueError):
        encode_version("v0.9.4.1.2")

    sig_dir = _sig_dir_for(tmp_path, _LITE_INSTALLERS)
    manifest, missing = build_manifest(
        tag=TAG,
        variant="lite",
        assets=_assets_for(_LITE_INSTALLERS),
        sig_dir=str(sig_dir),
        allow_missing=set(),
    )

    assert missing == []
    assert manifest is not None
    assert manifest["version"] == "0.9.4+1"


def test_manifest_maps_every_platform_from_asset_names(tmp_path: Path) -> None:
    """Every LITE platform PLATFORM_PATTERNS declares gets a signature + url."""

    sig_dir = _sig_dir_for(tmp_path, _LITE_INSTALLERS)
    manifest, missing = build_manifest(
        tag=TAG,
        variant="lite",
        assets=_assets_for(_LITE_INSTALLERS),
        sig_dir=str(sig_dir),
        allow_missing=set(),
    )

    assert missing == []
    assert manifest is not None
    assert set(manifest["platforms"]) == set(PLATFORM_PATTERNS["lite"])
    for platform, installer_name in _LITE_INSTALLERS.items():
        entry = manifest["platforms"][platform]
        assert entry["url"].endswith(installer_name)
        assert entry["signature"]  # the .sig file's contents, non-empty


def test_missing_signature_fails_loud(tmp_path: Path) -> None:
    """A platform with an installer asset but NO readable .sig is a hard failure, named."""

    sig_dir = _sig_dir_for(tmp_path, _LITE_INSTALLERS)
    # Delete just the darwin-aarch64 signature; its installer asset stays listed.
    (sig_dir / f"{_LITE_INSTALLERS['darwin-aarch64']}.sig").unlink()

    manifest, missing = build_manifest(
        tag=TAG,
        variant="lite",
        assets=_assets_for(_LITE_INSTALLERS),
        sig_dir=str(sig_dir),
        allow_missing=set(),
    )

    assert manifest is None
    assert missing == ["darwin-aarch64"]

    out = tmp_path / "latest-lite.json"
    assets_file = tmp_path / "assets.json"
    assets_file.write_text(json.dumps(_assets_for(_LITE_INSTALLERS)), encoding="utf-8")
    rc = main(
        [
            "--tag",
            TAG,
            "--variant",
            "lite",
            "--out",
            str(out),
            "--assets-json",
            str(assets_file),
            "--sig-dir",
            str(sig_dir),
        ]
    )

    assert rc == 1
    assert not out.exists()


def test_missing_installer_asset_is_also_a_named_failure(tmp_path: Path) -> None:
    """A platform with no matching installer asset at all is named too (not just a missing sig)."""

    installers = dict(_LITE_INSTALLERS)
    del installers["linux-aarch64"]
    sig_dir = _sig_dir_for(tmp_path, installers)

    manifest, missing = build_manifest(
        tag=TAG,
        variant="lite",
        assets=_assets_for(installers),
        sig_dir=str(sig_dir),
        allow_missing=set(),
    )

    assert manifest is None
    assert missing == ["linux-aarch64"]


def test_allow_missing_skips_a_legitimately_excluded_platform(tmp_path: Path) -> None:
    """--allow-missing lets an operator ship without a platform the matrix legitimately dropped."""

    installers = dict(_LITE_INSTALLERS)
    del installers["linux-aarch64"]
    sig_dir = _sig_dir_for(tmp_path, installers)

    manifest, missing = build_manifest(
        tag=TAG,
        variant="lite",
        assets=_assets_for(installers),
        sig_dir=str(sig_dir),
        allow_missing={"linux-aarch64"},
    )

    assert missing == []
    assert manifest is not None
    assert "linux-aarch64" not in manifest["platforms"]
    assert set(manifest["platforms"]) == set(installers)


def test_lite_variant_uses_lite_assets(tmp_path: Path) -> None:
    """With BOTH bundled and lite assets in the same release, --variant lite picks the lite ones.

    The bundled windows-x86_64 name (``..._x64-setup-bundled.exe``) must never satisfy the lite
    pattern (``..._x64-setup.exe$``), and vice versa -- proves the -bundled token really
    disambiguates the two variants' updater payloads.
    """

    # NOTE: _LITE_INSTALLERS and _BUNDLED_INSTALLERS share platform KEYS with
    # DIFFERENT filename VALUES -- {**a, **b} would collapse to one filename
    # per shared key and silently lose the other variant's .sig fixture, so
    # sig files are written from the concatenated VALUES of both dicts.
    all_assets = _assets_for(_LITE_INSTALLERS) + _assets_for(_BUNDLED_INSTALLERS)
    sig_dir = tmp_path / "sigs"
    sig_dir.mkdir()
    for name in [*_LITE_INSTALLERS.values(), *_BUNDLED_INSTALLERS.values()]:
        _write_sig(sig_dir, name)

    manifest, missing = build_manifest(
        tag=TAG,
        variant="lite",
        assets=all_assets,
        sig_dir=str(sig_dir),
        allow_missing=set(),
    )

    assert missing == []
    assert manifest is not None
    windows_url = manifest["platforms"]["windows-x86_64"]["url"]
    assert windows_url.endswith(_LITE_INSTALLERS["windows-x86_64"])
    assert "-bundled" not in windows_url
    darwin_url = manifest["platforms"]["darwin-aarch64"]["url"]
    assert darwin_url.endswith(_LITE_INSTALLERS["darwin-aarch64"])
    assert "-bundled" not in darwin_url


def test_bundled_variant_uses_bundled_assets(tmp_path: Path) -> None:
    """The mirror of the lite test: --variant bundled picks the -bundled-token assets."""

    all_assets = _assets_for(_LITE_INSTALLERS) + _assets_for(_BUNDLED_INSTALLERS)
    sig_dir = tmp_path / "sigs"
    sig_dir.mkdir()
    for name in [*_LITE_INSTALLERS.values(), *_BUNDLED_INSTALLERS.values()]:
        _write_sig(sig_dir, name)

    manifest, missing = build_manifest(
        tag=TAG,
        variant="bundled",
        assets=all_assets,
        sig_dir=str(sig_dir),
        allow_missing=set(),
    )

    assert missing == []
    assert manifest is not None
    assert set(manifest["platforms"]) == set(PLATFORM_PATTERNS["bundled"])
    windows_url = manifest["platforms"]["windows-x86_64"]["url"]
    assert windows_url.endswith(_BUNDLED_INSTALLERS["windows-x86_64"])


def test_main_writes_manifest_and_reports_platforms(tmp_path: Path, capsys: Any) -> None:
    """The CLI writes valid JSON and prints a success summary naming the platforms."""

    sig_dir = _sig_dir_for(tmp_path, _LITE_INSTALLERS)
    assets_file = tmp_path / "assets.json"
    assets_file.write_text(json.dumps(_assets_for(_LITE_INSTALLERS)), encoding="utf-8")
    out = tmp_path / "latest-lite.json"

    rc = main(
        [
            "--tag",
            TAG,
            "--variant",
            "lite",
            "--out",
            str(out),
            "--assets-json",
            str(assets_file),
            "--sig-dir",
            str(sig_dir),
        ]
    )

    assert rc == 0
    written = json.loads(out.read_text(encoding="utf-8"))
    assert written["version"] == "0.9.4+1"
    assert set(written["platforms"]) == set(PLATFORM_PATTERNS["lite"])
    assert "CHANGELOG" in written["notes"]
    captured = capsys.readouterr()
    assert "OK: wrote" in captured.out
