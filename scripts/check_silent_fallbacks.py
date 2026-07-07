#!/usr/bin/env python3
"""Ratchet guard against silent fallbacks (iowarp/clio-agent#772).

Counts the ruff violations that mark invisible degraded paths --
``BLE001`` (blind ``except Exception``), ``S110`` (``try``/``except``/``pass``)
and ``E722`` (bare ``except:``) -- across ``src/clio_agent`` and compares the
total against a recorded baseline. The check FAILS (exit 1) only when the
total EXCEEDS the baseline, so new silent fallbacks cannot land while the
existing inventory is burned down.

The baseline lives in :data:`BASELINE_TOTAL` below and may only move DOWN:
when a burn-down lowers the live count, update the constant to the new total
in the same change, ratcheting the guarantee tighter over time. Sites that
keep a *justified* blind ``except`` must log a structured ``reason=`` warning
(the ``gact/streaming.py`` stream-fallback house style) and carry an explicit
``# noqa: BLE001 - <why>`` so the suppression is visible in review.

Run as part of CI (initially in warn-only mode) and locally::

    uv run python scripts/check_silent_fallbacks.py
    uv run python scripts/check_silent_fallbacks.py --path src/clio_agent --baseline 200
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

# Ruff rules that mark a silent-fallback site.
RATCHET_RULES: tuple[str, ...] = ("BLE001", "S110", "E722")

# Recorded baseline: the known count of silent-fallback sites in SRC_ROOT.
# This number may only ratchet DOWN (iowarp/clio-agent#772). Update it in the
# same change that burns sites down; never raise it.
BASELINE_TOTAL = 159

# Root of the source tree to scan, relative to the repository root.
SRC_ROOT = "src/clio_agent"

# Matches one ``--statistics`` line, e.g. ``173\tBLE001\tblind-except``.
_STAT_LINE = re.compile(r"^\s*(\d+)\s+([A-Z]+\d+)\b")


def _repo_root() -> Path:
    """Return the repository root (parent of the ``scripts`` directory)."""
    return Path(__file__).resolve().parent.parent


def count_violations(path: Path, rules: tuple[str, ...] = RATCHET_RULES) -> dict[str, int]:
    """Return ``{rule: count}`` for the ratcheted rules under ``path``.

    Runs ``ruff check --statistics --isolated`` (isolated so the count cannot
    be silently lowered by editing the project ruff config; per-line ``noqa``
    directives still apply) and parses the per-rule counts.

    Raises:
        RuntimeError: if ruff itself fails (exit code other than 0/1).
    """
    proc = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [
            sys.executable,
            "-m",
            "ruff",
            "check",
            "--select",
            ",".join(rules),
            "--statistics",
            "--isolated",
            "--quiet",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode not in (0, 1):
        raise RuntimeError(
            f"ruff check failed (exit {proc.returncode}): {proc.stderr.strip() or proc.stdout.strip()}"
        )
    counts = dict.fromkeys(rules, 0)
    for line in proc.stdout.splitlines():
        match = _STAT_LINE.match(line)
        if match is not None and match.group(2) in counts:
            counts[match.group(2)] = int(match.group(1))
    return counts


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Return 0 if at/below baseline, 1 if above."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--path",
        type=Path,
        default=_repo_root() / SRC_ROOT,
        help=f"Directory to scan (default: <repo>/{SRC_ROOT}).",
    )
    parser.add_argument(
        "--baseline",
        type=int,
        default=BASELINE_TOTAL,
        help=f"Maximum allowed total (default: {BASELINE_TOTAL}).",
    )
    args = parser.parse_args(argv)

    counts = count_violations(args.path)
    total = sum(counts.values())

    print(f"silent-fallback ratchet (#772) over {args.path}:")
    for rule in RATCHET_RULES:
        print(f"  {rule}: {counts[rule]}")
    print(f"  total: {total} (baseline: {args.baseline})")

    if total > args.baseline:
        print(
            f"FAIL: {total} silent-fallback site(s) exceed the recorded baseline "
            f"of {args.baseline} (#772). Fix the new site -- log a structured "
            "reason= warning or surface the reason in result metadata -- instead "
            "of raising the baseline."
        )
        return 1
    if total < args.baseline:
        print(
            f"OK: count dropped below the baseline -- ratchet it down: set "
            f"BASELINE_TOTAL = {total} in scripts/check_silent_fallbacks.py."
        )
    else:
        print("OK: silent-fallback count is at the recorded baseline.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
