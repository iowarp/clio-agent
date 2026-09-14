"""Create or verify verbatim blueprint snapshots for the v0.9.2 campaign."""

from __future__ import annotations

import argparse
import filecmp
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
TARGET = Path(__file__).resolve().parents[1] / "blueprints"
SOURCES = {
    "base-agent": ROOT / "external" / "clio-agent-marketplace" / "base-agent",
    "earthscope-single-agent": (
        ROOT / "external" / "clio-agent-marketplace" / "earthscope-single-agent"
    ),
    "factorio-flat": ROOT / "external" / "clio-agent-marketplace" / "factorio-flat",
    "tool-ui-qualification-workflow": (
        ROOT
        / "scripts"
        / "live_verification"
        / "tool_presentation"
        / "blueprints"
        / "tool-ui-qualification-workflow"
    ),
    "tool-ui-cross-file-triage": (
        ROOT
        / "scripts"
        / "live_verification"
        / "tool_presentation"
        / "blueprints"
        / "tool-ui-cross-file-triage"
    ),
    "v2ex-avenues": ROOT / "scripts" / "live_verification" / "agents" / "v2ex-avenues",
}


def files_equal(source: Path, target: Path) -> bool:
    """Return whether two directory trees contain identical file bytes."""

    comparison = filecmp.dircmp(source, target)
    if comparison.left_only or comparison.right_only or comparison.funny_files:
        return False
    if any(
        not filecmp.cmp(source / name, target / name, shallow=False)
        for name in comparison.common_files
    ):
        return False
    return all(files_equal(source / name, target / name) for name in comparison.common_dirs)


def main() -> int:
    """Snapshot source blueprints or verify existing snapshots."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.check:
        mismatches = [
            name for name, source in SOURCES.items() if not files_equal(source, TARGET / name)
        ]
        if mismatches:
            raise SystemExit(f"blueprint snapshots differ: {', '.join(mismatches)}")
        print(f"verified {len(SOURCES)} blueprint snapshots")
        return 0

    TARGET.mkdir(parents=True, exist_ok=True)
    for name, source in SOURCES.items():
        destination = TARGET / name
        if destination.exists():
            shutil.rmtree(destination)
        shutil.copytree(source, destination, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        print(f"snapshotted {name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
