"""Deterministic quality-gate fixture for the Tool UI qualification campaign."""

from __future__ import annotations

import argparse
import sys
import tomllib
from pathlib import Path


def load_strict_mode(settings_path: Path) -> bool:
    """Return whether strict verification is enabled in the fixture settings."""
    with settings_path.open("rb") as stream:
        payload = tomllib.load(stream)
    return bool(payload.get("gate", {}).get("strict", False))


def main() -> int:
    """Run the requested deterministic gate stage and return its process status."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--stage", choices=("lint", "verify"), required=True)
    parser.add_argument(
        "--settings",
        type=Path,
        default=Path(__file__).with_name("settings.toml"),
    )
    args = parser.parse_args()

    strict = load_strict_mode(args.settings)
    if args.stage == "lint":
        print("quality gate warning: strict verification is disabled", file=sys.stderr)
        print("lint completed with 1 warning")
        return 0

    if not strict:
        print("quality gate failed: gate.strict must be true", file=sys.stderr)
        print("verification stopped before publish")
        return 2

    print("quality gate passed: strict verification is enabled")
    print("verification completed with 0 failures")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

