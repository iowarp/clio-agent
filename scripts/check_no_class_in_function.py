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
  hidden classes) must match its *recorded* violation count exactly. More
  ("regressed") fails; fewer ("padded") fails until the entry is lowered, so a
  lifted class cannot be traded for a new one later (#1203); an entry whose
  file no longer exists ("stale") fails until it is removed.

The baseline may only ratchet DOWN (house precedent:
``check_silent_fallbacks.py::BASELINE_TOTAL``). The PR that lifts a hidden class
lowers the file's entry (or removes it once the file has zero violations); the
check fails until it does.

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
# ratchet DOWN, and each entry must equal the file's real count -- when a class
# is lifted to module scope, lower its number here (or drop the entry once the
# file reaches zero) in the same change, or the check fails. Paths are
# relative to the repository root, forward slashes.
RATCHET_BASELINE: dict[str, int] = {
    # (agent_elicitation_answer_inline.py's entry retired, #1331 review round:
    # the hidden ``_AgentAnswer`` dspy.Signature class was HOISTED to module
    # scope -- the fix is to hoist a class out of a function, not to record
    # and baseline-exempt it. Zero violations now.)
    "src/clio_agent/gact/agents/builders.py": 3,
    "src/clio_agent/gact/app.py": 1,
    "src/clio_agent/lm/adapters.py": 2,
    "src/clio_agent/providers/argonne_auth.py": 1,
    "src/clio_agent/runtime/lm_activity.py": 1,
}

# Root of the source tree to scan, relative to the repository root.
SRC_ROOT = "src/clio_agent"


class Failure(NamedTuple):
    """A file or baseline entry that breaks the ratchet (fails the check)."""

    rel: str
    count: int  # 0 for "stale": the file does not exist
    kind: str  # "new" | "regressed" | "padded" | "stale"
    baseline: int  # recorded count (0 for a non-baselined file)
    sites: list[tuple[int, str]]  # (lineno, classname) for reporting


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
) -> list[Failure]:
    """Evaluate the per-file class-in-function ratchet under ``scan_root``.

    Args:
        scan_root: Directory tree to walk for ``*.py`` files.
        rel_to: Base directory used to compute the forward-slash relative path
            that keys into ``baseline``. Defaults to ``scan_root``.
        baseline: Per-file exact recorded violation counts. Defaults to
            :data:`RATCHET_BASELINE`.

    Returns:
        Every offense: a hidden class in a non-baselined file, a baselined file
        whose count differs from its entry in either direction, or an entry
        whose file no longer exists. Empty means the ratchet holds.
    """
    if baseline is None:
        baseline = RATCHET_BASELINE
    base = rel_to if rel_to is not None else scan_root

    failures: list[Failure] = []
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
            failures.append(Failure(rel, count, "padded", recorded, sites))
    for rel, recorded in baseline.items():
        if rel not in seen:
            failures.append(Failure(rel, 0, "stale", recorded, []))
    return failures


def _note(entry: Failure) -> str:
    """Why ``entry`` fails, and for a stale baseline the edit that fixes it."""
    if entry.kind == "new":
        return "new violation(s) in a non-baselined file"
    if entry.kind == "regressed":
        return f"regressed past recorded baseline {entry.baseline}"
    if entry.kind == "stale":
        return f"baselined at {entry.baseline} but the file no longer exists -- remove its entry"
    fix = "remove its entry" if entry.count == 0 else f"lower its entry to {entry.count}"
    return f"baseline claims {entry.baseline} -- {fix}"


def _print_report(failures: list[Failure]) -> None:
    """Print the ratchet report."""
    if not failures:
        print(f"OK: every class-in-function count under {SRC_ROOT} is exactly at its baseline.")
        return

    print(f"FAIL: {len(failures)} entry(ies) break the class-in-function ratchet (#714, #774):")
    for entry in failures:
        print(f"  {entry.rel}: {entry.count} ({_note(entry)})")
        for lineno, name in entry.sites:
            print(f"    {entry.rel}:{lineno}:{name}")
    print("Baselines live in RATCHET_BASELINE in scripts/check_no_class_in_function.py.")


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Return 0 if the ratchet holds, 1 on any failure."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args(argv)

    repo_root = _repo_root()
    failures = check_no_class_in_function(repo_root / SRC_ROOT, rel_to=repo_root)
    _print_report(failures)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
