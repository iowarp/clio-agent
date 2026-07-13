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
import os
import re
import subprocess
import sys
from pathlib import Path

# Ruff rules that mark a silent-fallback site.
RATCHET_RULES: tuple[str, ...] = ("BLE001", "S110", "E722")

# Recorded baseline: the known count of silent-fallback sites in SRC_ROOT.
# This number may only ratchet DOWN (iowarp/clio-agent#772). Update it in the
# same change that burns sites down; never raise it.
BASELINE_TOTAL = 1

# Root of the source tree to scan, relative to the repository root.
SRC_ROOT = "src/clio_agent"

# Matches one ``--statistics`` line, e.g. ``173\tBLE001\tblind-except``.
_STAT_LINE = re.compile(r"^\s*(\d+)\s+([A-Z]+\d+)\b")

# Any leading-count line ``--statistics`` could emit. Used only to distinguish a
# statistics-shaped line we failed to fully parse (a format/colour regression)
# from genuinely empty output, so the miscount cannot be swallowed as "clean".
_STAT_LIKE = re.compile(r"^\s*\d")

# CSI escape sequences (colour/formatting). ruff colours ``--statistics`` output
# when FORCE_COLOR/CLICOLOR_FORCE are set; a coloured count like
# ``\x1b[1m1\x1b[0m\t\x1b[1;31mBLE001\x1b[0m`` defeats :data:`_STAT_LINE` and
# would silently zero the count. Strip these before parsing (defense in depth --
# the subprocess is also launched with colour forced off).
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


def _repo_root() -> Path:
    """Return the repository root (parent of the ``scripts`` directory)."""
    return Path(__file__).resolve().parent.parent


def count_violations(path: Path, rules: tuple[str, ...] = RATCHET_RULES) -> dict[str, int]:
    """Return ``{rule: count}`` for the ratcheted rules under ``path``.

    Runs ``ruff check --statistics --isolated`` (isolated so the count cannot
    be silently lowered by editing the project ruff config; per-line ``noqa``
    directives still apply) and parses the per-rule counts.

    The subprocess is launched with colour forced off (``NO_COLOR`` set,
    ``FORCE_COLOR``/``CLICOLOR_FORCE`` removed) and ANSI escapes are stripped
    from the output before parsing, so an environment that colours ruff's
    ``--statistics`` output cannot make the parser silently return zeros.

    Raises:
        RuntimeError: if ruff itself fails (exit code other than 0/1); if any
            statistics-shaped line fails to parse into a known rule -- regardless
            of the total, so a heterogeneous partial parse (one good line, one
            mangled) cannot let the un-parsed rule read as zero; or if ruff
            reports violations (exit 1) yet nothing parsed at all (a total
            wipeout that would otherwise read as a clean tree).
    """
    env = os.environ.copy()
    env["NO_COLOR"] = "1"
    env.pop("FORCE_COLOR", None)
    env.pop("CLICOLOR_FORCE", None)
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
        env=env,
    )
    if proc.returncode not in (0, 1):
        raise RuntimeError(
            f"ruff check failed (exit {proc.returncode}): {proc.stderr.strip() or proc.stdout.strip()}"
        )
    stdout = _ANSI_RE.sub("", proc.stdout)
    counts = dict.fromkeys(rules, 0)
    suspicious: list[str] = []
    for line in stdout.splitlines():
        if not line.strip():
            continue
        match = _STAT_LINE.match(line)
        if match is not None:
            # A well-formed statistics line. Record it when it names a rule we
            # selected; a well-formed line for an UNSELECTED rule is valid output,
            # not a parse failure, so it is ignored (never flagged suspicious).
            if match.group(2) in counts:
                counts[match.group(2)] = int(match.group(1))
        elif _STAT_LIKE.match(line):
            # Statistics-SHAPED (leading count) yet it did NOT parse into a rule
            # code at all: a format/colour regression that would silently zero
            # whichever selected rule this line was reporting.
            suspicious.append(line)
    # NO SILENT FALLBACK: a statistics-shaped line we could not resolve to a known
    # rule means at least one selected rule's count is being read as zero when it
    # may not be. Raise regardless of the total -- a heterogeneous partial parse
    # (one good line parses, sum>0, one mangled line does not) still hides an
    # undercount, the exact class this guard exists to catch (#772).
    if suspicious:
        raise RuntimeError(
            "ruff emitted statistics-shaped output that did not parse into a "
            "known rule -- the format changed or was mangled beyond ANSI "
            "stripping, so the affected rule would be silently counted as zero. "
            f"Suspicious line(s): {suspicious!r}. Raw stdout: {proc.stdout!r}"
        )
    # A total wipeout: ruff says violations exist (exit 1) yet nothing parsed and
    # nothing was even statistics-shaped -- the output vanished entirely, which
    # would otherwise read as a clean tree.
    if proc.returncode == 1 and sum(counts.values()) == 0:
        raise RuntimeError(
            "ruff reported violations (exit 1) but no statistics line parsed -- "
            "the output format changed or was mangled beyond ANSI stripping. "
            f"Raw stdout: {proc.stdout!r}"
        )
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
