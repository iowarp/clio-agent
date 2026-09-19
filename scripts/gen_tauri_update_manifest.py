#!/usr/bin/env python3
"""Build and validate the CLIO desktop Tauri updater manifest (``latest.json``).

Signed desktop auto-update (v0.9.4.1) needs one static manifest per desktop
variant -- ``latest.json`` (bundled) and ``latest-lite.json`` (lite) -- served
from the GitHub release's own assets, per Tauri's updater plugin schema::

    {"version": "0.9.4+1", "notes": "...", "pub_date": "<RFC3339>",
     "platforms": {"windows-x86_64": {"signature": "...", "url": "..."}, ...}}

This script reads a release's uploaded asset names (``gh release view <tag>
--json assets``, or ``--assets-json <file>`` for tests), maps them onto the
updater's platform keys using the SAME staged-name conventions
``clio-bundles.yml``'s "Stage artifacts" step produces (the ``+N -> .N``
version rename, the ``-bundled`` token, and the target-triple suffix added to
the otherwise-unversioned macOS ``.app.tar.gz``), fetches each matched
``.sig`` file's contents (its asset download URL, or ``--sig-dir <dir>`` for
tests), and writes the manifest.

Only the platforms the ``clio-bundles.yml`` desktop matrix actually produces
an UPDATE-CAPABLE artifact for, per variant, are required (see
:data:`PLATFORM_PATTERNS`): Windows-on-ARM never bundles at all
(``--no-bundle``, no NSIS output, either variant); the bundled variant's
Linux legs drop AppImage (``--bundles deb,rpm``, no updater payload); the
bundled variant never builds ``x86_64-apple-darwin`` at all (matrix
``exclude``, no clio-core wheel for that host). A platform this script DOES
require, with no matching installer asset or ``.sig``, is a hard failure
naming it -- no silent partial manifest ever ships (the cleanup program's
no-silent-fallback ground rule). ``--allow-missing`` is an explicit,
operator-invoked escape hatch for a platform that is normally required but
is known to be absent for this specific release.

Usage (CI, real release)::

    python3 scripts/gen_tauri_update_manifest.py --tag v0.9.4.1 \\
        --variant bundled --out latest.json
    python3 scripts/gen_tauri_update_manifest.py --tag v0.9.4.1 \\
        --variant lite --out latest-lite.json

Usage (tests, fully offline)::

    python3 scripts/gen_tauri_update_manifest.py --tag v0.9.4.1 --variant lite \\
        --out latest-lite.json --assets-json assets.json --sig-dir sigs/
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from release_notes_from_changelog import section_for  # noqa: E402

REPO_URL = "https://github.com/iowarp/clio-agent"

#: Update-payload platform key -> installer-asset-name regex, one map per
#: desktop variant, matched with ``re.search`` against staged asset names.
#: Each pattern's ``.sig`` sibling is derived by :func:`_sig_pattern`. Payload
#: kind per platform matches the updater's OWN artifact type, not the human
#: installer: Windows -> the NSIS ``-setup.exe`` (never the ``.msi``, which
#: Tauri's updater does not sign), macOS -> the raw ``.app.tar.gz`` (never
#: the ``.dmg``), Linux -> the ``.AppImage`` (never ``.deb``/``.rpm``).
PLATFORM_PATTERNS: dict[str, dict[str, str]] = {
    "bundled": {
        "windows-x86_64": r"_x64-setup-bundled\.exe$",
        "darwin-aarch64": r"aarch64-apple-darwin-bundled\.app\.tar\.gz$",
    },
    "lite": {
        "windows-x86_64": r"_x64-setup\.exe$",
        "darwin-aarch64": r"aarch64-apple-darwin\.app\.tar\.gz$",
        "darwin-x86_64": r"x86_64-apple-darwin\.app\.tar\.gz$",
        "linux-x86_64": r"_amd64\.AppImage$",
        "linux-aarch64": r"_aarch64\.AppImage$",
    },
}


def encode_version(tag: str) -> str:
    """Map a CLIO release tag to the SemVer the desktop app was built with.

    Mirrors ``clio-bundles.yml``'s merge script exactly: a three-part release
    (``vX.Y.Z``) keeps plain SemVer; a four-part CLIO maintenance release
    (``vX.Y.Z.N``) encodes ``N`` as numeric SemVer BUILD METADATA
    (``X.Y.Z+N``) -- Rust's ``semver`` orders numeric build metadata, so
    ``0.9.4 < 0.9.4+1 < 0.9.5``.

    Raises:
        ValueError: ``tag`` (after stripping a leading ``v``) is not a valid
            three- or four-part CLIO release version.
    """

    version = tag.lstrip("v")
    maintenance = re.match(r"^(\d+\.\d+\.\d+)\.(\d+)$", version)
    if maintenance:
        return f"{maintenance.group(1)}+{maintenance.group(2)}"
    if not re.match(r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$", version):
        raise ValueError(f"invalid CLIO release version: {version!r}")
    return version


def _sig_pattern(installer_pattern: str) -> str:
    """Derive an installer pattern's detached-signature pattern (``<x>.sig$``)."""

    assert installer_pattern.endswith("$"), installer_pattern
    return installer_pattern[:-1] + r"\.sig$"


def _find_asset(assets: list[dict[str, Any]], pattern: str) -> dict[str, Any] | None:
    """Return the single asset whose ``name`` matches ``pattern``, or ``None``.

    Raises:
        ValueError: more than one asset matches (an ambiguous pattern is a
            bug in this script, not a release defect -- fail loud rather than
            silently pick one).
    """

    compiled = re.compile(pattern)
    matches = [asset for asset in assets if compiled.search(str(asset.get("name", "")))]
    if len(matches) > 1:
        names = ", ".join(sorted(str(m.get("name", "")) for m in matches))
        raise ValueError(f"ambiguous match for /{pattern}/: {names}")
    return matches[0] if matches else None


def _load_assets(tag: str, assets_json: str | None) -> list[dict[str, Any]]:
    """Return the release's asset list: from ``--assets-json`` or a live ``gh`` call."""

    if assets_json is not None:
        raw = Path(assets_json).read_text(encoding="utf-8")
    else:
        result = subprocess.run(
            ["gh", "release", "view", tag, "--json", "assets"],
            capture_output=True,
            text=True,
            check=True,
        )
        raw = result.stdout
    data = json.loads(raw)
    assets = data["assets"] if isinstance(data, dict) else data
    if not isinstance(assets, list):
        raise ValueError("release asset listing did not decode to a list")
    return assets


def _read_signature(sig_asset: dict[str, Any], sig_dir: str | None) -> str | None:
    """Return a ``.sig`` asset's contents, or ``None`` on any failure to read it.

    From ``--sig-dir/<name>`` in tests; from the asset's own download ``url``
    (an ordinary HTTPS GET, no auth required for a public release asset) in
    CI. Never raises -- a failure here is reported as a missing platform by
    the caller, never a stack trace.
    """

    name = str(sig_asset.get("name", ""))
    if sig_dir is not None:
        path = Path(sig_dir) / name
        if not path.is_file():
            return None
        return path.read_text(encoding="utf-8").strip()
    url = str(sig_asset.get("url", ""))
    if not url:
        return None
    try:
        with urllib.request.urlopen(url, timeout=30) as response:  # noqa: S310
            return response.read().decode("utf-8").strip()
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return None


def _rfc3339_now() -> str:
    """Return the current UTC instant as an RFC3339 timestamp (``...Z`` form)."""

    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _notes_for(public_version: str) -> str | None:
    """Return the manifest's ``notes`` body: the CHANGELOG section plus a link.

    Reuses :func:`release_notes_from_changelog.section_for` -- the SAME
    CHANGELOG section the release-page backstop publishes, so the updater's
    in-app notes and the GitHub release notes never diverge. Returns ``None``
    when the tag has no CHANGELOG section (the caller fails loud rather than
    shipping a manifest with placeholder notes).
    """

    body = section_for(public_version)
    if body is None:
        return None
    return (
        body + "\n\n**Full details:** "
        f"[CHANGELOG {public_version}]({REPO_URL}/blob/main/CHANGELOG.md)"
    )


def build_manifest(
    *,
    tag: str,
    variant: str,
    assets: list[dict[str, Any]],
    sig_dir: str | None,
    allow_missing: set[str],
) -> tuple[dict[str, Any] | None, list[str]]:
    """Build the manifest dict for ``variant``, or report the platforms it is missing.

    Returns:
        ``(manifest, missing)``. Exactly one of the two is meaningful:
        ``manifest`` is ``None`` when ``missing`` is non-empty (a required
        platform has no installer asset, no ``.sig`` asset, or an unreadable
        ``.sig``); otherwise ``missing`` is empty and ``manifest`` is the
        complete, ready-to-write manifest dict.
    """

    patterns = PLATFORM_PATTERNS[variant]
    platforms: dict[str, dict[str, str]] = {}
    missing: list[str] = []

    for platform in sorted(patterns):
        installer_pattern = patterns[platform]
        installer = _find_asset(assets, installer_pattern)
        sig_asset = _find_asset(assets, _sig_pattern(installer_pattern))
        signature = _read_signature(sig_asset, sig_dir) if sig_asset is not None else None
        if installer is None or sig_asset is None or signature is None:
            if platform not in allow_missing:
                missing.append(platform)
            continue
        platforms[platform] = {"signature": signature, "url": str(installer.get("url", ""))}

    if missing:
        return None, missing

    public_version = tag.lstrip("v")
    notes = _notes_for(public_version)
    if notes is None:
        return None, [f"<no CHANGELOG section for {public_version!r}>"]

    manifest = {
        "version": encode_version(tag),
        "notes": notes,
        "pub_date": _rfc3339_now(),
        "platforms": platforms,
    }
    return manifest, []


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Returns 0 on a complete manifest, 1 on any missing platform."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", required=True, help="Release tag, e.g. v0.9.4.1")
    parser.add_argument("--variant", required=True, choices=sorted(PLATFORM_PATTERNS))
    parser.add_argument("--out", required=True, metavar="PATH", help="Manifest output path")
    parser.add_argument(
        "--assets-json",
        metavar="PATH",
        default=None,
        help="Release asset listing (gh release view --json assets shape); defaults to a live "
        "`gh release view` call.",
    )
    parser.add_argument(
        "--sig-dir",
        metavar="DIR",
        default=None,
        help="Read .sig contents from DIR/<asset name> instead of downloading them.",
    )
    parser.add_argument(
        "--allow-missing",
        metavar="PLATFORM,PLATFORM,...",
        default="",
        help="Comma-separated platform keys to skip (never hard-fail on) if their installer or "
        "signature is absent from this release.",
    )
    args = parser.parse_args(argv)

    allow_missing = {p.strip() for p in args.allow_missing.split(",") if p.strip()}
    assets = _load_assets(args.tag, args.assets_json)
    manifest, missing = build_manifest(
        tag=args.tag,
        variant=args.variant,
        assets=assets,
        sig_dir=args.sig_dir,
        allow_missing=allow_missing,
    )

    if manifest is None:
        print(
            f"FAIL: {args.variant} update manifest for {args.tag} is missing: "
            + ", ".join(missing),
            file=sys.stderr,
        )
        return 1

    Path(args.out).write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    platform_list = ", ".join(sorted(manifest["platforms"]))
    print(
        f"OK: wrote {args.out} ({args.variant}, version {manifest['version']}): "
        f"{len(manifest['platforms'])} platform(s) -> {platform_list}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
