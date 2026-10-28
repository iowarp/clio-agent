#!/usr/bin/env python3
"""Baseline-0 guard: no middle dot ("·", U+00B7) in server copy.

Product rule: the UI never shows a middle dot. gact-tui enforces it for its own
sources (``web/src/lib/no-middle-dot-copy.test.ts``); this guard covers the text
the server builds and sends to the UI (tool presentations, reference details,
directory labels, session exports).

Every string literal under ``src/clio_agent`` is checked, f-string pieces and
escaped spellings (``\\u00b7``, ``\\N{MIDDLE DOT}``) included, because the
parser resolves them to the same character. Comments and docstrings are skipped:
they never reach the UI, and a docstring may quote upstream output verbatim. A
literal that must keep the character (for example to match a third-party
message) carries ``# allow-middle-dot: <reason>`` on its line.

Run as part of CI (blocking) and locally::

    uv run python scripts/check_no_middle_dot_copy.py
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path
from typing import NamedTuple

SCAN_ROOTS: tuple[str, ...] = ("src/clio_agent",)
MIDDLE_DOT = "·"
ALLOW_MARKER = "# allow-middle-dot:"


class Violation(NamedTuple):
    """A string literal containing a middle dot."""

    rel: str
    line: int
    text: str


def _repo_root() -> Path:
    """Return the repository root (parent of the ``scripts`` directory)."""
    return Path(__file__).resolve().parent.parent


def _docstring_ids(tree: ast.AST) -> set[int]:
    """Return the ids of bare string-expression statements (docstrings)."""
    return {
        id(node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    }


def scan_source(text: str) -> list[tuple[int, str]]:
    """Return ``(line, literal)`` for every non-docstring literal with a middle dot.

    A file that does not parse is reported on every line that holds the
    character outside a comment, so a syntax error cannot hide a violation.
    """
    lines = text.splitlines()
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return [
            (lineno, raw.strip())
            for lineno, raw in enumerate(lines, start=1)
            if MIDDLE_DOT in raw.split("#", 1)[0]
        ]
    docstrings = _docstring_ids(tree)
    hits: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
            continue
        if id(node) in docstrings or MIDDLE_DOT not in node.value:
            continue
        first, last = node.lineno, getattr(node, "end_lineno", None) or node.lineno
        if any(ALLOW_MARKER in lines[i - 1] for i in range(first, last + 1)):
            continue
        hits.append((first, node.value.strip()[:80]))
    return sorted(set(hits))


def check_no_middle_dot_copy(scan_root: Path, *, rel_to: Path | None = None) -> list[Violation]:
    """Scan every ``*.py`` file under ``scan_root``.

    Args:
        scan_root: Directory tree to walk.
        rel_to: Base directory for the reported forward-slash relative path.

    Returns:
        Every :class:`Violation` found (empty when the tree is clean).
    """
    base = rel_to if rel_to is not None else scan_root
    violations: list[Violation] = []
    if not scan_root.is_dir():
        return violations
    for path in sorted(scan_root.rglob("*.py")):
        text = path.read_text(encoding="utf-8", errors="replace")
        if MIDDLE_DOT not in text and "00b7" not in text.lower() and "MIDDLE DOT" not in text:
            continue
        rel = path.relative_to(base).as_posix()
        violations.extend(Violation(rel, line, literal) for line, literal in scan_source(text))
    return violations


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Return 0 when the tree is clean, 1 on any violation."""
    del argv
    repo_root = _repo_root()
    violations: list[Violation] = []
    for root in SCAN_ROOTS:
        violations.extend(check_no_middle_dot_copy(repo_root / root, rel_to=repo_root))
    if not violations:
        print(f"OK: no middle dot in string literals under {', '.join(SCAN_ROOTS)} (baseline 0).")
        return 0
    print(f"FAIL: {len(violations)} string literal(s) contain a middle dot:")
    for entry in violations:
        print(f"  {entry.rel}:{entry.line}: {entry.text!r}")
    print(
        "The UI never shows a middle dot. Rewrite the text in plain words: commas for "
        "runs of facts, parentheses for counts and qualifiers, a colon for 'Kind: state', "
        "or separate fields. If the literal must match third-party output, add "
        f"'{ALLOW_MARKER} <reason>' on its line."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
