#!/usr/bin/env python3
"""Guard against the class-in-function anti-pattern in clio_agent.

Defining a ``class`` inside a function body hides structure: the type is
invisible to importers, cannot be referenced for typing, is re-created on every
call, and tends to grow into a private mini-framework buried in a closure. The
gact decomposition (iowarp/clio-agent#714) is moving such hidden classes up to
module scope so they become real, importable, testable units.

This check parses ``src/clio_agent/**/*.py`` with the stdlib ``ast`` module and
fails (exit 1) if any ``ClassDef`` is nested -- at any depth -- inside a
``FunctionDef`` or ``AsyncFunctionDef``, UNLESS the file is on the allowlist.
``app.py`` is allowlisted for now because several of its factory functions
still define classes-in-functions until later decomposition phases lift them
out. Remove entries as they are cleaned up, ratcheting the guarantee tighter.

Run as part of CI (initially in warn-only mode) and locally::

    uv run python scripts/check_no_class_in_function.py
    uv run python scripts/check_no_class_in_function.py --allow src/clio_agent/foo.py
"""

from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path

# Files permitted to contain classes-in-functions because they are actively
# being decomposed. Paths are relative to the repository root, forward slashes.
# Remove entries as the hidden classes are lifted to module scope (#714).
ALLOWLIST: list[str] = [
    "src/clio_agent/gact/app.py",
]

# Root of the source tree to scan, relative to the repository root.
SRC_ROOT = "src/clio_agent"


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


def check_no_class_in_function(allowlist: set[str]) -> list[tuple[str, int, str]]:
    """Return ``(relpath, lineno, classname)`` for every violation.

    A violation is a ``ClassDef`` nested inside a ``FunctionDef`` /
    ``AsyncFunctionDef`` in a non-allowlisted file (paths relative to the repo
    root, forward slashes).
    """
    repo_root = _repo_root()
    src_root = repo_root / SRC_ROOT
    violations: list[tuple[str, int, str]] = []
    for path in sorted(src_root.rglob("*.py")):
        rel = path.relative_to(repo_root).as_posix()
        if rel in allowlist:
            continue
        source = path.read_text(encoding="utf-8", errors="replace")
        try:
            tree = ast.parse(source, filename=str(path))
        except SyntaxError as exc:  # pragma: no cover - surfaced as a hard error
            print(f"ERROR: could not parse {rel}: {exc}", file=sys.stderr)
            raise
        visitor = _ClassInFunctionVisitor()
        visitor.visit(tree)
        for lineno, name in visitor.violations:
            violations.append((rel, lineno, name))
    return violations


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Return 0 if clean, 1 if any violation is found."""
    parser = argparse.ArgumentParser(description=__doc__)
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
    violations = check_no_class_in_function(allowlist)

    if not violations:
        print(f"OK: no class-in-function under {SRC_ROOT}.")
        return 0

    print(f"FAIL: {len(violations)} class-in-function violation(s) (#714):")
    for rel, lineno, name in violations:
        print(f"  {rel}:{lineno}:{name}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
