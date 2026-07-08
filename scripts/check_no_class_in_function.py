#!/usr/bin/env python3
"""Ratchet guard against the class-in-function anti-pattern in clio_agent.

Defining a ``class`` inside a function body hides structure: the type is
invisible to importers, cannot be referenced for typing, is re-created on every
call, and tends to grow into a private mini-framework buried in a closure. The
gact decomposition (iowarp/clio-agent#714, #767) is moving such hidden classes
up to module scope so they become real, importable, testable units.

This check parses ``src/clio_agent/**/*.py`` with the stdlib ``ast`` module and
enforces a per-file ratchet on the number of ``ClassDef`` nodes nested -- at
any depth -- inside a ``FunctionDef`` / ``AsyncFunctionDef``:

* A file **not** in :data:`RATCHET_BASELINE` may have **zero** such violations
  -- a new hidden class anywhere fails the check.
* A file **in** :data:`RATCHET_BASELINE` (the modules still carrying known
  hidden classes) may not exceed its *recorded* violation count.

The baseline may only ratchet DOWN (house precedent:
``check_silent_fallbacks.py::BASELINE_TOTAL``). When a file loses a hidden
class, the check reports the ratchet-down and the same PR that lifted the class
updates :data:`RATCHET_BASELINE` (lowering the number, or removing the entry
once the file has zero violations). Ratchet-down reports are advisory: they do
not fail the build.

Run as part of CI (blocking) and locally::

    uv run python scripts/check_no_class_in_function.py
"""

from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path
from typing import NamedTuple

# Per-file ratchet baseline: modules still carrying hidden classes, at their
# current violation counts (iowarp/clio-agent#714, #767). This mapping may only
# ratchet DOWN -- when a class is lifted to module scope, lower its number here
# (or drop the entry once the file reaches zero) in the same change. Paths are
# relative to the repository root, forward slashes.
RATCHET_BASELINE: dict[str, int] = {
    "src/clio_agent/gact/agents/builders.py": 5,
    "src/clio_agent/gact/agents/runtime.py": 1,
    "src/clio_agent/gact/app.py": 1,
    "src/clio_agent/lm/adapters.py": 2,
    "src/clio_agent/lm/io_logging.py": 1,
    "src/clio_agent/providers/argonne_auth.py": 1,
    "src/clio_agent/runtime/lm_activity.py": 1,
}

# Root of the source tree to scan, relative to the repository root.
SRC_ROOT = "src/clio_agent"


class Failure(NamedTuple):
    """A file that breaks the ratchet (fails the check)."""

    rel: str
    count: int
    kind: str  # "new" (non-baselined has violations) or "regressed" (over recorded)
    baseline: int  # recorded count (0 for a non-baselined file)
    sites: list[tuple[int, str]]  # (lineno, classname) for reporting


class RatchetDown(NamedTuple):
    """A baselined file that lost violations -- advisory, not a failure."""

    rel: str
    count: int
    baseline: int
    cleared: bool  # True once count == 0 (drop the entry entirely)


class Result(NamedTuple):
    """Outcome of a scan: failures fail the build, ratchet_downs are advisory."""

    failures: list[Failure]
    ratchet_downs: list[RatchetDown]


def _repo_root() -> Path:
    """Return the repository root (parent of the ``scripts`` directory)."""
    return Path(__file__).resolve().parent.parent


class _ClassInFunctionVisitor(ast.NodeVisitor):
    """Collect every ``ClassDef`` that is nested inside a function."""

    def __init__(self) -> None:
        self._function_depth = 0
        self.violations: list[tuple[int, str]] = []

    def _visit_function(self, node: ast.AST) -> None:
        self._function_depth += 1
        self.generic_visit(node)
        self._function_depth -= 1

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        if self._function_depth > 0:
            self.violations.append((node.lineno, node.name))
        # Recurse: a method of this class could itself define a nested class.
        self.generic_visit(node)


def _violations_in(path: Path) -> list[tuple[int, str]]:
    """Return ``(lineno, classname)`` for every class-in-function in ``path``."""
    source = path.read_text(encoding="utf-8", errors="replace")
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as exc:  # pragma: no cover - surfaced as a hard error
        print(f"ERROR: could not parse {path}: {exc}", file=sys.stderr)
        raise
    visitor = _ClassInFunctionVisitor()
    visitor.visit(tree)
    return visitor.violations


def check_no_class_in_function(
    scan_root: Path,
    *,
    rel_to: Path | None = None,
    baseline: dict[str, int] | None = None,
) -> Result:
    """Evaluate the per-file class-in-function ratchet under ``scan_root``.

    Args:
        scan_root: Directory tree to walk for ``*.py`` files.
        rel_to: Base directory used to compute the forward-slash relative path
            that keys into ``baseline``. Defaults to ``scan_root``.
        baseline: Per-file recorded violation counts. Defaults to
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
    seen: set[str] = set()
    for path in sorted(scan_root.rglob("*.py")):
        rel = path.relative_to(base).as_posix()
        seen.add(rel)
        sites = _violations_in(path)
        count = len(sites)
        recorded = baseline.get(rel)
        if recorded is None:
            if count > 0:
                failures.append(Failure(rel, count, "new", 0, sites))
            continue
        if count > recorded:
            failures.append(Failure(rel, count, "regressed", recorded, sites))
        elif count < recorded:
            ratchet_downs.append(
                RatchetDown(rel, count, recorded, cleared=count == 0)
            )

    # A baselined file that vanished (deleted/renamed) is also a ratchet-down:
    # its recorded violations are gone. Report so the stale entry gets removed.
    for rel, recorded in baseline.items():
        if rel not in seen:
            ratchet_downs.append(RatchetDown(rel, 0, recorded, cleared=True))

    return Result(failures=failures, ratchet_downs=ratchet_downs)


def _print_report(result: Result) -> None:
    """Print the ratchet report (failures then advisory ratchet-downs)."""
    for entry in result.ratchet_downs:
        if entry.cleared:
            print(
                f"OK (ratchet down): {entry.rel} now has no class-in-function "
                "-- remove it from RATCHET_BASELINE in "
                "scripts/check_no_class_in_function.py."
            )
        else:
            print(
                f"OK (ratchet down): {entry.rel} dropped {entry.baseline} -> "
                f"{entry.count} violation(s) -- lower its RATCHET_BASELINE entry "
                f"to {entry.count} in scripts/check_no_class_in_function.py."
            )

    if not result.failures:
        print(f"OK: no class-in-function under {SRC_ROOT} exceeds its ratchet baseline.")
        return

    total = sum(len(entry.sites) for entry in result.failures)
    print(
        f"FAIL: {total} class-in-function violation(s) break the ratchet "
        f"(#714, #774):"
    )
    for entry in result.failures:
        if entry.kind == "new":
            note = "new violation(s) in a non-baselined file"
        else:
            note = f"regressed past recorded baseline {entry.baseline}"
        print(f"  {entry.rel}: {entry.count} ({note}):")
        for lineno, name in entry.sites:
            print(f"    {entry.rel}:{lineno}:{name}")


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Return 0 if the ratchet holds, 1 on any failure."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args(argv)

    repo_root = _repo_root()
    result = check_no_class_in_function(repo_root / SRC_ROOT, rel_to=repo_root)
    _print_report(result)
    return 1 if result.failures else 0


if __name__ == "__main__":
    sys.exit(main())
