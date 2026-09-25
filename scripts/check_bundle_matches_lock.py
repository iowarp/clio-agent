#!/usr/bin/env python3
"""Fail the build if a shipped runtime's resolved packages drift from ``uv.lock``.

Root cause this guards (CU, iowarp/clio-agent#CU): the bundled CLIO Desktop
runtime and the plain installer both build their Python environment with
``uv pip install <spec> ...`` -- a fresh, unlocked resolution against
whatever is on PyPI at BUILD TIME, not ``uv sync``/``uv sync --frozen``. A
released bundle can therefore ship a DIFFERENT ``iowarp-core`` (or any other
dependency) than the one CI tested and ``uv.lock`` pins. That mismatch caused
a real incident: a 2.1.0 clio-core client talking to a 2.2.1 daemon (or vice
versa) on one host, because two install paths resolved independently.

This script is the release-time backstop: after a runtime is built (bundled
Desktop, or any other ``uv pip install``-based environment), compare every
package it actually installed against ``uv.lock``'s pinned version for that
same package. Any mismatch fails loudly with a diff table -- silence is not
an option (#775 no-silent-fallback). It complements, not replaces, the
``--constraint`` file the build scripts now pass to ``uv pip install`` (see
``install/build-gact-runtime.sh`` / ``.ps1``): the constraint pins the
resolution, this script proves the constraint actually took effect.

Usage::

    uv run python scripts/check_bundle_matches_lock.py --python <path/to/python>
    uv run python scripts/check_bundle_matches_lock.py --python <path> --lock other/uv.lock
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tomllib
from pathlib import Path

# The lockfile's own [[package]] entry for the project root (clio-agent itself)
# is intentionally excluded: a bundled/dev build installs it from a local
# checkout or git ref, never from the lock's own (often absent) version pin,
# so comparing it would be a false positive on every build.
_EXCLUDED_PACKAGES = frozenset({"clio-agent"})


def load_lock_versions(lock_path: Path) -> dict[str, str]:
    """Return ``{normalized_package_name: version}`` from a ``uv.lock`` file.

    Raises:
        FileNotFoundError: If ``lock_path`` does not exist.
        ValueError: If the file has no ``[[package]]`` entries (malformed lock).
    """
    data = tomllib.loads(lock_path.read_text(encoding="utf-8"))
    packages = data.get("package", [])
    if not packages:
        raise ValueError(f"{lock_path} has no [[package]] entries -- malformed lock")
    versions: dict[str, str] = {}
    for entry in packages:
        name = entry.get("name")
        version = entry.get("version")
        if not name or not version:
            continue  # a workspace member without a resolved version (e.g. path source)
        versions[_normalize(name)] = version
    return versions


def load_installed_versions(python: Path) -> dict[str, str]:
    """Return ``{normalized_package_name: version}`` installed at ``python``.

    Uses ``uv pip list`` (not ``pip``) because the bundled runtime deliberately
    ships no ``pip`` console script or guaranteed importable ``pip`` module
    (console-script shims embed build-host paths and are deleted, see
    ``install/build-gact-runtime.sh``); ``uv`` introspects the target
    interpreter's site-packages without needing anything installed there.
    """
    proc = subprocess.run(  # noqa: S603 - fixed argv, no shell, trusted local uv binary
        ["uv", "pip", "list", "--python", str(python), "--format", "json"],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"'uv pip list --python {python}' failed (rc={proc.returncode}): {proc.stderr.strip()}"
        )
    rows = json.loads(proc.stdout)
    return {_normalize(row["name"]): row["version"] for row in rows}


def _normalize(name: str) -> str:
    """PEP 503 package-name normalization so ``iowarp-core``/``iowarp_core`` compare equal."""
    return name.lower().replace("_", "-").replace(".", "-")


def find_mismatches(
    locked: dict[str, str], installed: dict[str, str]
) -> list[tuple[str, str, str]]:
    """Return ``[(package, locked_version, installed_version)]`` for every disagreement.

    Only packages present in BOTH sets are compared: a package the bundle installs
    that is not one of clio-agent's own locked dependencies (clio-kit, fastmcp-slim,
    uv itself, ...) is out of scope for this guard, not a silent pass on a real gap.
    """
    mismatches = []
    for name, locked_version in sorted(locked.items()):
        if name in _EXCLUDED_PACKAGES:
            continue
        installed_version = installed.get(name)
        if installed_version is not None and installed_version != locked_version:
            mismatches.append((name, locked_version, installed_version))
    return mismatches


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--python", required=True, type=Path, help="Path to the built runtime's python executable"
    )
    parser.add_argument(
        "--lock",
        type=Path,
        default=Path(__file__).resolve().parent.parent / "uv.lock",
        help="Path to uv.lock (default: repo root uv.lock)",
    )
    args = parser.parse_args()

    if not args.python.is_file():
        print(f"check_bundle_matches_lock: python not found at {args.python}", file=sys.stderr)
        return 1
    if not args.lock.is_file():
        print(f"check_bundle_matches_lock: lock not found at {args.lock}", file=sys.stderr)
        return 1

    locked = load_lock_versions(args.lock)
    installed = load_installed_versions(args.python)

    # iowarp-core is the named concern (CU) -- always report its state explicitly,
    # even when it matches, so a green run still shows the check actually ran.
    core_locked = locked.get("iowarp-core")
    core_installed = installed.get("iowarp-core")
    print(f"iowarp-core: locked={core_locked!r} installed={core_installed!r}")

    mismatches = find_mismatches(locked, installed)
    if mismatches:
        print(
            f"FAIL: {len(mismatches)} package(s) in the built runtime diverge from {args.lock}:",
            file=sys.stderr,
        )
        for name, locked_version, installed_version in mismatches:
            print(
                f"  {name}: locked={locked_version} installed={installed_version}", file=sys.stderr
            )
        print(
            "This means the build resolved fresh instead of honoring the lock -- check "
            "the --constraint file passed to 'uv pip install' in the build script.",
            file=sys.stderr,
        )
        return 1

    print(f"OK: every locked package present in {args.python} matches {args.lock}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
