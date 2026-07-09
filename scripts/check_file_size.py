#!/usr/bin/env python3
"""Ratchet guard against god-files in the clio_agent source tree.

This check exists to prevent re-accretion of monolithic modules now that the
gact decomposition (iowarp/clio-agent#714, #767) has landed. It walks
``src/clio_agent/**/*.py`` and enforces a per-file line-count ratchet:

* A file **not** in :data:`RATCHET_BASELINE` may not exceed
  :data:`DEFAULT_MAX_LINES` -- a brand-new god-file fails the check.
* A file **in** :data:`RATCHET_BASELINE` (the known-oversized modules still
  awaiting decomposition) may not exceed its *recorded* line count -- it can
  shrink but never grow past where it is today.

The baseline may only ratchet DOWN (house precedent:
``check_silent_fallbacks.py::BASELINE_TOTAL``). When a file is brought under
the cap, or merely shrinks, the check reports the ratchet-down and the same PR
that shrank it updates :data:`RATCHET_BASELINE` (lowering the number, or
removing the entry once the file is under ``DEFAULT_MAX_LINES``). Ratchet-down
reports are advisory: they do not fail the build.

Run as part of CI (blocking) and locally::

    uv run python scripts/check_file_size.py
    uv run python scripts/check_file_size.py --max 600
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import NamedTuple

# Default maximum number of lines a single *non-baselined* source module may
# contain. New files must stay under this cap.
DEFAULT_MAX_LINES = 800

# Per-file ratchet baseline: the known-oversized modules at their current line
# counts, recorded so they cannot regrow. These are the files awaiting further
# decomposition (iowarp/clio-agent#714, #767). This mapping may only ratchet
# DOWN -- when a file shrinks, lower its number here (or drop the entry once it
# falls under DEFAULT_MAX_LINES) in the same change. Paths are relative to the
# repository root and use forward slashes.
RATCHET_BASELINE: dict[str, int] = {
    "src/clio_agent/agent.py": 2715,
    "src/clio_agent/arc/memory.py": 1394,
    "src/clio_agent/arc/segments.py": 1117,
    "src/clio_agent/arc/storage.py": 978,
    "src/clio_agent/gact/agent_blueprints.py": 1100,
    "src/clio_agent/gact/agents/builders.py": 2209,
    "src/clio_agent/gact/agents/resolution.py": 803,
    "src/clio_agent/gact/app.py": 2527,
    "src/clio_agent/gact/delegation.py": 960,
    "src/clio_agent/gact/routes/agents.py": 921,
    "src/clio_agent/gact/routes/blueprints.py": 859,
    "src/clio_agent/gact/routes/catalog.py": 880,
    "src/clio_agent/gact/routes/mcp.py": 939,
    "src/clio_agent/gact/routes/providers.py": 1314,
    "src/clio_agent/gact/routes/sessions.py": 1478,
    "src/clio_agent/gact/runtime/globals.py": 923,
    "src/clio_agent/gact/streaming.py": 1027,
    "src/clio_agent/gact/tool_observer.py": 977,
    "src/clio_agent/gact/transcript.py": 996,
    "src/clio_agent/gact/turn.py": 816,
    "src/clio_agent/gact/turn_delegation.py": 1014,
    "src/clio_agent/gact/turn_finalize.py": 966,
    "src/clio_agent/gact/types.py": 1143,
    "src/clio_agent/providers/claude_code_litellm.py": 1176,
    "src/clio_agent/runtime/status.py": 1222,
    "src/clio_agent/tools/execution.py": 1187,
    "src/clio_agent/ui/cli.py": 1156,
}

# Root of the source tree to scan, relative to the repository root.
SRC_ROOT = "src/clio_agent"


class Failure(NamedTuple):
    """A file that breaks the ratchet (fails the check)."""

    rel: str
    count: int
    kind: str  # "new" (non-baselined over cap) or "regressed" (over recorded)
    limit: int  # the cap it broke (DEFAULT_MAX_LINES or the recorded baseline)


class RatchetDown(NamedTuple):
    """A baselined file that shrank -- advisory, not a failure."""

    rel: str
    count: int
    baseline: int
    under_cap: bool  # True once count <= max_lines (drop the entry entirely)


class Result(NamedTuple):
    """Outcome of a scan: failures fail the build, ratchet_downs are advisory."""

    failures: list[Failure]
    ratchet_downs: list[RatchetDown]


def _repo_root() -> Path:
    """Return the repository root (parent of the ``scripts`` directory)."""
    return Path(__file__).resolve().parent.parent


def _count_lines(path: Path) -> int:
    """Return the number of lines in ``path``."""
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        return sum(1 for _ in handle)


def check_file_size(
    scan_root: Path,
    *,
    rel_to: Path | None = None,
    max_lines: int = DEFAULT_MAX_LINES,
    baseline: dict[str, int] | None = None,
) -> Result:
    """Evaluate the per-file line-count ratchet under ``scan_root``.

    Args:
        scan_root: Directory tree to walk for ``*.py`` files.
        rel_to: Base directory used to compute the forward-slash relative path
            that keys into ``baseline``. Defaults to ``scan_root``.
        max_lines: Cap applied to files not present in ``baseline``.
        baseline: Per-file recorded line counts. Defaults to
            :data:`RATCHET_BASELINE`.

    Returns:
        A :class:`Result` splitting build-failing offenders from advisory
        ratchet-down reports.
    """
    if baseline is None:
        baseline = RATCHET_BASELINE
    base = rel_to if rel_to is not None else scan_root

    failures: list[Failure] = []
    ratchet_downs: list[RatchetDown] = []
    for path in sorted(scan_root.rglob("*.py")):
        rel = path.relative_to(base).as_posix()
        count = _count_lines(path)
        recorded = baseline.get(rel)
        if recorded is None:
            if count > max_lines:
                failures.append(Failure(rel, count, "new", max_lines))
            continue
        if count > recorded:
            failures.append(Failure(rel, count, "regressed", recorded))
        elif count < recorded:
            ratchet_downs.append(
                RatchetDown(rel, count, recorded, under_cap=count <= max_lines)
            )
    return Result(failures=failures, ratchet_downs=ratchet_downs)


def _print_report(result: Result, max_lines: int) -> None:
    """Print the ratchet report (failures then advisory ratchet-downs)."""
    for entry in result.ratchet_downs:
        if entry.under_cap:
            print(
                f"OK (ratchet down): {entry.rel} is now {entry.count} lines "
                f"(<= {max_lines}) -- remove it from RATCHET_BASELINE in "
                "scripts/check_file_size.py."
            )
        else:
            print(
                f"OK (ratchet down): {entry.rel} shrank {entry.baseline} -> "
                f"{entry.count} -- lower its RATCHET_BASELINE entry to "
                f"{entry.count} in scripts/check_file_size.py."
            )

    if not result.failures:
        print(
            f"OK: no file under {SRC_ROOT} exceeds its ratchet baseline "
            f"(cap {max_lines} for new files)."
        )
        return

    print(f"FAIL: {len(result.failures)} file(s) break the size ratchet (#714, #774):")
    for entry in result.failures:
        if entry.kind == "new":
            print(f"  {entry.rel}:{entry.count} (new file exceeds cap {entry.limit})")
        else:
            print(
                f"  {entry.rel}:{entry.count} (regressed past recorded "
                f"baseline {entry.limit})"
            )


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Return 0 if the ratchet holds, 1 on any failure."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--max",
        type=int,
        default=DEFAULT_MAX_LINES,
        help=f"Cap for non-baselined files (default: {DEFAULT_MAX_LINES}).",
    )
    args = parser.parse_args(argv)

    repo_root = _repo_root()
    result = check_file_size(
        repo_root / SRC_ROOT,
        rel_to=repo_root,
        max_lines=args.max,
    )
    _print_report(result, args.max)
    return 1 if result.failures else 0


if __name__ == "__main__":
    sys.exit(main())
