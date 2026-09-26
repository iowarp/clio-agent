#!/usr/bin/env python3
"""Keep the bundled CLIO Desktop runtime on exactly the ``uv.lock`` set.

Root cause this guards (CU): the bundled runtime is built with
``uv pip install``, a fresh resolution at BUILD TIME, not ``uv sync --frozen``.
Two defects followed from that:

* a package installed outside the lock (a hardcoded ``clio-kit==2.10.6``)
  was reconciled with the locked set only at release time, where its
  ``click>=8.3.3`` floor contradicted the locked ``click==8.3.0`` and the
  build failed outright;
* the constraint file exported fewer extras than the bundle installs, so
  packages from the missing extras resolved unpinned (``pytz`` drifted).

The fix is one install set, defined by :data:`BUNDLE_EXTRAS` (and the matching
``BUNDLE_EXTRAS`` list in ``install/build-gact-runtime.sh`` / ``.ps1``): the
build installs ``clio-agent[<extras>]`` against a ``uv export`` of those same
extras. This script has two modes built on that set:

``--python <runtime python>`` (release-time backstop, default)
    Compare every package the built runtime installed against the lock. It
    fails on a version that differs from ``uv.lock`` AND on a package the
    lock export for :data:`BUNDLE_EXTRAS` does not cover at all (an unlocked
    package is drift too, not an out-of-scope pass).

``--resolve`` (every PR, no runtime needed)
    Export the constraints and resolve the bundle's exact install set against
    them for every bundle target platform (``uv pip compile``), so a conflict
    like the clio-kit/click one fails on the PR instead of at release.

Usage::

    python scripts/check_bundle_matches_lock.py --python <path/to/python>
    python scripts/check_bundle_matches_lock.py --resolve

The script needs only the standard library and ``uv`` on PATH, so it runs with
the bundled interpreter itself.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

#: The extras the bundled runtime installs. MUST equal ``BUNDLE_EXTRAS`` in
#: install/build-gact-runtime.sh and .ps1 (a test enforces this).
BUNDLE_EXTRAS: tuple[str, ...] = ("argonne", "desktop")

#: The targets clio-bundles.yml builds the BUNDLED variant for (its matrix
#: excludes x86_64-apple-darwin and aarch64-pc-windows-msvc: iowarp-core ships no
#: wheels there, so those targets ship lite only).
BUNDLE_TARGETS: tuple[str, ...] = (
    "x86_64-pc-windows-msvc",
    "aarch64-apple-darwin",
    "x86_64-unknown-linux-gnu",
    "aarch64-unknown-linux-gnu",
)

#: macOS deployment target of the bundle runner (macos-14). uv's darwin triples
#: default to 13.0, below iowarp-core's macosx_14_0 wheel tag.
BUNDLE_MACOSX_DEPLOYMENT_TARGET = "14.0"

#: The bundled runtime's interpreter version (build-gact-runtime default).
BUNDLE_PYTHON = "3.12"

#: Installed distributions that are not lock-resolved packages:
#: ``clio-agent`` is installed from the local checkout (the lock has no
#: registry version for it) and ``web-mcp`` is clio-kit's Web Search adapter,
#: installed from clio-kit's data directory (its dependencies ARE locked via
#: the ``desktop`` extra and are checked like any other package).
LOCAL_PROJECTS = frozenset({"clio-agent", "web-mcp"})

#: Distributions shipped INSIDE the python-build-standalone interpreter the
#: runtime copies (its seeded ``pip``), present before any resolve runs. They
#: are listed by name in the check's output, never silently skipped.
INTERPRETER_SEEDED = frozenset({"pip"})

#: Always reported by name, even on a green run, so the log shows the check ran
#: against the packages whose drift caused real incidents.
REPORTED_PACKAGES: tuple[str, ...] = ("iowarp-core", "clio-kit", "click")


def normalize(name: str) -> str:
    """PEP 503 package-name normalization so ``iowarp-core``/``iowarp_core`` compare equal."""
    return name.lower().replace("_", "-").replace(".", "-")


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
        versions[normalize(name)] = version
    return versions


def _run_uv(args: list[str], env: dict[str, str] | None = None) -> str:
    """Run ``uv`` with ``args`` (and optional extra environment) and return stdout.

    Raises:
        RuntimeError: If uv exits non-zero; the message carries uv's stderr.
    """
    proc = subprocess.run(  # noqa: S603 - fixed argv, no shell, trusted local uv binary
        ["uv", *args],  # noqa: S607 - uv is resolved from PATH by design
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, **env} if env else None,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"'uv {' '.join(args)}' failed (rc={proc.returncode}): {proc.stderr.strip()}"
        )
    return proc.stdout


def export_args(project: Path, extras: tuple[str, ...], output: Path | None = None) -> list[str]:
    """Return the ``uv export`` argv (without ``uv``) the build scripts use for ``extras``."""
    args = ["export", "--project", str(project), "--frozen", "--no-hashes", "--no-emit-project"]
    for extra in extras:
        args += ["--extra", extra]
    if output is not None:
        args += ["-o", str(output)]
    return args


def parse_export_names(export_text: str) -> set[str]:
    """Return the normalized package names in a ``uv export`` requirements text.

    Handles ``name==version ; marker`` and ``name @ url ; marker`` lines and
    skips comments, blank lines, indented ``# via`` annotations and options.
    """
    names: set[str] = set()
    for raw in export_text.splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", "-")) or raw[:1].isspace():
            continue
        requirement = line.split(";", 1)[0].strip()
        for separator in ("==", " @ ", "@"):
            if separator in requirement:
                requirement = requirement.split(separator, 1)[0]
                break
        requirement = requirement.split("[", 1)[0].strip()
        if requirement:
            names.add(normalize(requirement))
    return names


def load_bundle_scope(project: Path, extras: tuple[str, ...]) -> set[str]:
    """Return the package names ``uv export`` emits for the bundle's extras."""
    return parse_export_names(_run_uv(export_args(project, extras)))


def load_installed_versions(python: Path) -> dict[str, str]:
    """Return ``{normalized_package_name: version}`` installed at ``python``.

    Uses ``uv pip list`` (not ``pip``) because the bundled runtime deliberately
    ships no ``pip`` console script (console-script shims embed build-host
    paths and are deleted, see ``install/build-gact-runtime.sh``); ``uv``
    introspects the target interpreter's site-packages directly.
    """
    rows = json.loads(_run_uv(["pip", "list", "--python", str(python), "--format", "json"]))
    return {normalize(row["name"]): row["version"] for row in rows}


def find_drift(
    locked: dict[str, str], installed: dict[str, str], scope: set[str]
) -> list[tuple[str, str | None, str]]:
    """Return ``[(package, locked_version_or_None, installed_version)]`` drift rows.

    A row with ``locked_version`` set is a version mismatch against ``uv.lock``;
    a row with ``None`` is a package the lock export for the bundle's extras
    does not cover (installed from an unlocked resolution).
    """
    drift: list[tuple[str, str | None, str]] = []
    for name, installed_version in sorted(installed.items()):
        if name in LOCAL_PROJECTS or name in INTERPRETER_SEEDED:
            continue
        if name not in scope or name not in locked:
            drift.append((name, None, installed_version))
        elif locked[name] != installed_version:
            drift.append((name, locked[name], installed_version))
    return drift


def verify_runtime(python: Path, lock: Path, project: Path) -> int:
    """Check a built runtime against the lock; return a process exit code."""
    locked = load_lock_versions(lock)
    scope = load_bundle_scope(project, BUNDLE_EXTRAS)
    installed = load_installed_versions(python)

    for name in REPORTED_PACKAGES:
        print(f"{name}: locked={locked.get(name)!r} installed={installed.get(name)!r}")
    for name in sorted((LOCAL_PROJECTS | INTERPRETER_SEEDED) & installed.keys()):
        print(f"not lock-resolved (local project / interpreter-seeded): {name}=={installed[name]}")

    drift = find_drift(locked, installed, scope)
    if drift:
        print(
            f"FAIL: {len(drift)} package(s) in the built runtime drift from {lock} "
            f"(extras: {','.join(BUNDLE_EXTRAS)}):",
            file=sys.stderr,
        )
        for name, locked_version, installed_version in drift:
            if locked_version is None:
                print(
                    f"  {name}: NOT in the lock export, installed={installed_version}",
                    file=sys.stderr,
                )
            else:
                print(
                    f"  {name}: locked={locked_version} installed={installed_version}",
                    file=sys.stderr,
                )
        print(
            "Every package the bundle ships must come from the lock: add it to the "
            "`desktop` extra in pyproject.toml and run `uv lock`, and keep BUNDLE_EXTRAS "
            "identical in the build scripts and this check.",
            file=sys.stderr,
        )
        return 1

    resolved = len(installed.keys() - LOCAL_PROJECTS - INTERPRETER_SEEDED)
    print(
        f"OK: all {resolved} lock-resolved packages in {python} match {lock} "
        f"(extras: {','.join(BUNDLE_EXTRAS)})."
    )
    return 0


def resolve_bundle(project: Path) -> int:
    """Resolve the bundle's install set against the lock export on every target."""
    failures = 0
    with tempfile.TemporaryDirectory(prefix="bundle-resolve-") as tmp:
        constraints = Path(tmp) / "constraints.txt"
        _run_uv(export_args(project, BUNDLE_EXTRAS, constraints))
        requirements = Path(tmp) / "bundle.in"
        requirements.write_text(
            f"clio-agent[{','.join(BUNDLE_EXTRAS)}] @ {project.resolve().as_uri()}\n",
            encoding="utf-8",
        )
        for target in BUNDLE_TARGETS:
            try:
                _run_uv(
                    [
                        "pip",
                        "compile",
                        "--quiet",
                        "--no-header",
                        "--constraint",
                        str(constraints),
                        "--python-version",
                        BUNDLE_PYTHON,
                        "--python-platform",
                        target,
                        str(requirements),
                        "-o",
                        str(Path(tmp) / f"{target}.txt"),
                    ],
                    env={"MACOSX_DEPLOYMENT_TARGET": BUNDLE_MACOSX_DEPLOYMENT_TARGET},
                )
            except RuntimeError as exc:
                failures += 1
                print(f"FAIL [{target}]: {exc}", file=sys.stderr)
                continue
            print(f"OK [{target}]: clio-agent[{','.join(BUNDLE_EXTRAS)}] resolves on the lock")
    return 1 if failures else 0


def main() -> int:
    """Parse arguments and run the selected mode."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--python", type=Path, help="Path to the built runtime's python executable")
    mode.add_argument(
        "--resolve",
        action="store_true",
        help="Resolve the bundle install set against the lock for every bundle target",
    )
    parser.add_argument(
        "--project",
        type=Path,
        default=REPO_ROOT,
        help="clio-agent checkout whose pyproject.toml + uv.lock define the bundle",
    )
    parser.add_argument(
        "--lock", type=Path, default=None, help="Path to uv.lock (default: <project>/uv.lock)"
    )
    args = parser.parse_args()
    lock: Path = args.lock or args.project / "uv.lock"

    if not lock.is_file():
        print(f"check_bundle_matches_lock: lock not found at {lock}", file=sys.stderr)
        return 1
    try:
        if args.resolve:
            return resolve_bundle(args.project)
        if not args.python.is_file():
            print(f"check_bundle_matches_lock: python not found at {args.python}", file=sys.stderr)
            return 1
        return verify_runtime(args.python, lock, args.project)
    except RuntimeError as exc:
        print(f"check_bundle_matches_lock: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
