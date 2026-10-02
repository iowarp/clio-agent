#!/usr/bin/env python3
"""Move the user-updatable provider components to their latest releases (pyproject floor + uv.lock).

Run weekly by ``.github/workflows/provider-sdk-bump.yml`` (and on demand), so a
release always ships the newest Codex runtime and Claude Code SDK: the providers
gate new models by client version, and a release that ships a lagging component
hides models the user's account already serves.

A release counts only when it is final (no pre-release), not yanked, and ships
a wheel for EVERY bundle target (``BUNDLE_WHEEL_PLATFORMS``) -- claude-agent-sdk
0.2.157 and 0.2.160 shipped no Windows wheel, and an sdist installs without the
bundled ``claude`` CLI.

Usage::

    python scripts/bump_provider_sdks.py            # rewrite pyproject + uv lock
    python scripts/bump_provider_sdks.py --check    # report only, exit 0

Writes ``changed``, ``title`` and a multi-line ``body`` to ``$GITHUB_OUTPUT``
when it is set. Standard library only (plus ``uv`` on PATH for the lock).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
INDEX = "https://pypi.org/pypi"

#: Provider group -> (distributions, release notes URL). Mirrors
#: ``clio_agent.providers.components.registry.PROVIDER_COMPONENTS`` (a test
#: enforces the match; this script must run without CLIO installed).
GROUPS: dict[str, tuple[tuple[str, ...], str]] = {
    "codex": (("openai-codex-cli-bin",), "https://github.com/openai/codex/releases"),
    "claude_code": (
        ("claude-agent-sdk",),
        "https://github.com/anthropics/claude-agent-sdk-python/releases",
    ),
}

#: Wheel platform tag patterns, one per bundle target
#: (``check_bundle_matches_lock.BUNDLE_TARGETS``). A ``none-any`` wheel covers all.
BUNDLE_WHEEL_PLATFORMS: dict[str, re.Pattern[str]] = {
    "x86_64-pc-windows-msvc": re.compile(r"-win_amd64\.whl$"),
    "aarch64-apple-darwin": re.compile(r"-macosx_\d+_\d+_(arm64|universal2)\.whl$"),
    "x86_64-unknown-linux-gnu": re.compile(r"-manylinux[_\w]*x86_64\.whl$"),
    "aarch64-unknown-linux-gnu": re.compile(r"-manylinux[_\w]*aarch64\.whl$"),
}

_FINAL = re.compile(r"^\d+(?:\.\d+)*$")


def release_key(version: str) -> tuple[int, ...]:
    """Sort key of a final release (``0.157.1`` -> ``(0, 157, 1)``)."""
    return tuple(int(part) for part in version.split("."))


def fetch(distribution: str) -> dict[str, object]:
    """The PyPI JSON document of ``distribution``."""
    with urllib.request.urlopen(f"{INDEX}/{distribution}/json", timeout=30) as response:  # noqa: S310
        return json.load(response)


def covers_every_target(files: list[dict[str, object]]) -> bool:
    """Whether non-yanked wheels exist for every bundle target (or one pure wheel)."""
    names = [str(f.get("filename") or "") for f in files if not f.get("yanked")]
    wheels = [n for n in names if n.endswith(".whl")]
    if any(n.endswith("-none-any.whl") for n in wheels):
        return True
    return all(any(p.search(n) for n in wheels) for p in BUNDLE_WHEEL_PLATFORMS.values())


def shippable_versions(payload: dict[str, object]) -> set[str]:
    """Final releases that ship a wheel for every bundle target."""
    releases = payload.get("releases")
    if not isinstance(releases, dict):
        raise ValueError("PyPI reply has no releases")
    return {
        version
        for version, files in releases.items()
        if _FINAL.match(str(version)) and isinstance(files, list) and covers_every_target(files)
    }


def group_target(distributions: tuple[str, ...], payloads: dict[str, dict[str, object]]) -> str:
    """The newest shippable version of the group (every distribution ships it)."""
    common: set[str] | None = None
    for name in distributions:
        versions = shippable_versions(payloads[name])
        common = versions if common is None else common & versions
    return max(common or set(), key=release_key, default="")


def current_floors(pyproject: str) -> dict[str, str]:
    """``{distribution: floor}`` for every ``"<name>>=<version>"`` requirement of the groups."""
    floors: dict[str, str] = {}
    for distributions, _notes in GROUPS.values():
        for name in distributions:
            match = re.search(rf'"{re.escape(name)}>=([0-9.]+)"', pyproject)
            if match is None:
                raise ValueError(f'pyproject.toml declares no "{name}>=<version>" floor')
            floors[name] = match.group(1)
    return floors


def rewrite_floors(pyproject: str, targets: dict[str, str]) -> str:
    """Replace each group floor with its target version."""
    for name, version in targets.items():
        pyproject = re.sub(rf'"{re.escape(name)}>=[0-9.]+"', f'"{name}>={version}"', pyproject)
    return pyproject


def plan(payloads: dict[str, dict[str, object]], floors: dict[str, str]) -> dict[str, str]:
    """``{distribution: new version}`` for every distribution whose target beats its floor."""
    bumps: dict[str, str] = {}
    for distributions, _notes in GROUPS.values():
        target = group_target(distributions, payloads)
        if target and all(release_key(target) > release_key(floors[n]) for n in distributions):
            bumps.update(dict.fromkeys(distributions, target))
    return bumps


def describe(bumps: dict[str, str], floors: dict[str, str]) -> tuple[str, str]:
    """PR title and body for ``bumps``."""
    title = "chore(deps): bump provider SDKs to " + ", ".join(
        f"{n} {v}" for n, v in sorted(bumps.items())
    )
    lines = [
        "Weekly provider SDK bump (`scripts/bump_provider_sdks.py`). The providers gate new models by",
        "client version, so each release ships the newest SDKs; runtimes can also update these in place.",
        "",
        "| Package | From | To | Release notes |",
        "|---|---|---|---|",
    ]
    for group, (distributions, notes) in GROUPS.items():
        for name in distributions:
            if name in bumps:
                lines.append(f"| `{name}` ({group}) | {floors[name]} | {bumps[name]} | {notes} |")
    lines += [
        "",
        "Only final releases with a wheel for every bundle target are considered.",
        "CI runs on this branch; review the release notes before merging.",
    ]
    return title, "\n".join(lines)


def write_outputs(changed: bool, title: str, body: str) -> None:
    """Hand the result to the workflow (``$GITHUB_OUTPUT``) when running in Actions."""
    target = os.environ.get("GITHUB_OUTPUT")
    if not target:
        return
    with open(target, "a", encoding="utf-8") as handle:
        handle.write(f"changed={'true' if changed else 'false'}\n")
        handle.write(f"title={title}\n")
        handle.write(f"body<<__CLIO_BODY__\n{body}\n__CLIO_BODY__\n")


def main() -> int:
    """Compute the bump and apply it (unless ``--check``)."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--check", action="store_true", help="report only; change nothing")
    parser.add_argument("--project", type=Path, default=REPO_ROOT)
    args = parser.parse_args()
    pyproject_path = args.project / "pyproject.toml"
    pyproject = pyproject_path.read_text(encoding="utf-8")
    floors = current_floors(pyproject)
    payloads = {name: fetch(name) for distributions, _ in GROUPS.values() for name in distributions}
    bumps = plan(payloads, floors)
    if not bumps:
        print(
            "provider SDKs are current: "
            + ", ".join(f"{n}>={v}" for n, v in sorted(floors.items()))
        )
        write_outputs(False, "", "")
        return 0
    title, body = describe(bumps, floors)
    print(title)
    if args.check:
        write_outputs(True, title, body)
        return 0
    pyproject_path.write_text(rewrite_floors(pyproject, bumps), encoding="utf-8")
    upgrade = [arg for name in bumps for arg in ("--upgrade-package", f"{name}=={bumps[name]}")]
    subprocess.run(["uv", "lock", "--project", str(args.project), *upgrade], check=True)  # noqa: S603,S607
    write_outputs(True, title, body)
    return 0


if __name__ == "__main__":
    sys.exit(main())
