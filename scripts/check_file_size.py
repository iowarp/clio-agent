#!/usr/bin/env python3
"""Guard against god-files in the clio_agent source tree.

This check exists to prevent re-accretion of monolithic modules while the
gact package is decomposed (the ``app.py`` split, iowarp/clio-agent#714).
It walks ``src/clio_agent/**/*.py`` and fails (exit 1) if any file exceeds a
maximum line count, UNLESS that file is on the allowlist.

The allowlist is intentionally narrow: it carries the files that are *known*
to be oversized and are actively being decomposed. ``app.py`` is the prime
example -- it is ~24k lines and is being carved into ``runtime/``, ``agents/``,
``emit/``, ``routes/`` and friends. New modules created during that work MUST
stay under the cap so the decomposition does not simply move the monolith
around. As each allowlisted file is brought under the cap it should be removed
from the allowlist, ratcheting the guarantee tighter over time.

Run as part of CI (initially in warn-only mode) and locally::

    uv run python scripts/check_file_size.py
    uv run python scripts/check_file_size.py --max 600
    uv run python scripts/check_file_size.py --allow src/clio_agent/foo.py
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Default maximum number of lines a single source module may contain.
DEFAULT_MAX_LINES = 800

# Files permitted to exceed the cap because they are known-oversized and are
# actively being decomposed. Paths are relative to the repository root and use
# forward slashes. Remove entries as they are brought under the cap (#714).
ALLOWLIST: list[str] = [
    "src/clio_agent/gact/app.py",
]

# Root of the source tree to scan, relative to the repository root.
SRC_ROOT = "src/clio_agent"


def _repo_root() -> Path:
    """Return the repository root (parent of the ``scripts`` directory)."""
    return Path(__file__).resolve().parent.parent


def _count_lines(path: Path) -> int:
    """Return the number of lines in ``path``."""
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        return sum(1 for _ in handle)


def check_file_size(max_lines: int, allowlist: set[str]) -> list[tuple[str, int]]:
    """Return ``(relpath, linecount)`` for every offending file.

    A file offends if it has more than ``max_lines`` lines and is not present
    in ``allowlist`` (paths relative to the repo root, forward slashes).
    """
    repo_root = _repo_root()
    src_root = repo_root / SRC_ROOT
    offenders: list[tuple[str, int]] = []
    for path in sorted(src_root.rglob("*.py")):
        rel = path.relative_to(repo_root).as_posix()
        if rel in allowlist:
            continue
        line_count = _count_lines(path)
        if line_count > max_lines:
            offenders.append((rel, line_count))
    return offenders


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Return 0 if clean, 1 if any file offends."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--max",
        type=int,
        default=DEFAULT_MAX_LINES,
        help=f"Maximum lines per file (default: {DEFAULT_MAX_LINES}).",
    )
    parser.add_argument(
        "--allow",
        action="append",
        default=[],
        metavar="PATH",
        help=(
            "Additional repo-relative path to allowlist. May be repeated. "
            "Merged with the built-in allowlist."
        ),
    )
    args = parser.parse_args(argv)

    allowlist = set(ALLOWLIST) | {Path(p).as_posix() for p in args.allow}
    offenders = check_file_size(args.max, allowlist)

    if not offenders:
        print(f"OK: no file under {SRC_ROOT} exceeds {args.max} lines.")
        return 0

    print(f"FAIL: {len(offenders)} file(s) exceed {args.max} lines (#714):")
    for rel, line_count in offenders:
        print(f"  {rel}:{line_count}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
