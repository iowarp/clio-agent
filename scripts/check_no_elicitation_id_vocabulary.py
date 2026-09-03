#!/usr/bin/env python3
"""Baseline-0 guard: no ``elicitationId`` residue anywhere in the tree (C1-S4, #1284).

The 2026-07-28 MCP revision (SEP-2577) replaced the legacy elicitation
back-channel's per-request ``elicitationId`` correlation token -- along with
its paired completion notification -- with the generic Multi-Round-Trip
Request loop (``InputRequiredResult`` / ``inputResponses`` / ``requestState``,
opaque and re-issued verbatim each round). The client obligations table
(``docs/design/mcp-client-obligations-2026-07-28.md`` row F3) is explicit:
"elicitationId + completion notification REMOVED". CLIO's own elicitation
bridge (``gact/elicitation_bridge.py``) correlates purely by protocol
identity + the shared question-store id (``UserQuestion.id``) and by the
MRTR driver's own ``requestState`` -- it never needed, and must never grow,
a parallel ``elicitationId`` construct. This guard prevents that removed
vocabulary from creeping back in as a "helpful" correlation field.

Run as part of CI (blocking) and locally::

    uv run python scripts/check_no_elicitation_id_vocabulary.py
"""

from __future__ import annotations

import io
import sys
import tokenize
from pathlib import Path
from typing import NamedTuple

#: Both roots are scanned: the removed wire construct must never resurface in
#: production code OR in a test fixture that would then need it to pass.
SCAN_ROOTS: tuple[str, ...] = ("src/clio_agent", "tests")

#: The removed wire field, plus its natural Python (snake_case) spelling --
#: the same identifier a reintroducing PR would actually type.
BANNED_IDENTIFIERS: tuple[str, ...] = (
    "elicitationId",
    "elicitation_id",
)


class Violation(NamedTuple):
    """A banned token found in a scanned source file."""

    rel: str
    line: int
    token: str


def _repo_root() -> Path:
    """Return the repository root (parent of the ``scripts`` directory)."""
    return Path(__file__).resolve().parent.parent


def scan_source(text: str) -> list[tuple[int, str]]:
    """Return ``(line, matched_token)`` for every banned token in CODE.

    ``COMMENT`` tokens are skipped so a comment explaining the removal (like
    this file's own docstring) never false-positives; every other token
    (identifiers, string literals, docstrings) is checked. A file that fails
    to tokenize is scanned line-by-line, comment-stripped, as a conservative
    fallback (same contract as ``check_no_settle_vocabulary.py``).
    """

    hits: list[tuple[int, str]] = []
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(text).readline))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        for lineno, raw in enumerate(text.splitlines(), start=1):
            code = raw.split("#", 1)[0]
            for needle in BANNED_IDENTIFIERS:
                if needle in code:
                    hits.append((lineno, needle))
        return hits
    for tok in tokens:
        if tok.type == tokenize.COMMENT:
            continue
        for needle in BANNED_IDENTIFIERS:
            if needle in tok.string:
                hits.append((tok.start[0], needle))
    return hits


def check_no_elicitation_id_vocabulary(
    scan_root: Path, *, rel_to: Path | None = None
) -> list[Violation]:
    """Evaluate the baseline-0 ``elicitationId`` guard under ``scan_root``.

    Args:
        scan_root: Directory tree to walk for ``*.py`` files.
        rel_to: Base directory for the reported forward-slash relative path.

    Returns:
        Every :class:`Violation` found (empty when the tree is clean). This
        script itself, and the design docs that document the removal, are
        never scanned (only ``*.py`` under ``scan_root``), so citing the
        banned string in prose is always safe.
    """

    base = rel_to if rel_to is not None else scan_root
    violations: list[Violation] = []
    if not scan_root.is_dir():
        return violations
    for path in sorted(scan_root.rglob("*.py")):
        rel = path.relative_to(base).as_posix()
        text = path.read_text(encoding="utf-8", errors="replace")
        for line, token in scan_source(text):
            violations.append(Violation(rel, line, token))
    return violations


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Return 0 when the tree is clean, 1 on any violation."""

    repo_root = _repo_root()
    violations: list[Violation] = []
    for root in SCAN_ROOTS:
        violations.extend(check_no_elicitation_id_vocabulary(repo_root / root, rel_to=repo_root))
    if not violations:
        print(
            "OK: no elicitationId residue under "
            f"{', '.join(SCAN_ROOTS)} (baseline 0, SEP-2577 / #1284)."
        )
        return 0
    print(f"FAIL: {len(violations)} elicitationId vocabulary hit(s) (SEP-2577 / #1284):")
    for entry in violations:
        print(f"  {entry.rel}:{entry.line} reintroduced banned token {entry.token!r}")
    print(
        "The modern-era MRTR loop (InputRequiredResult / inputResponses / "
        "requestState) replaced elicitationId + its completion notification "
        "(client obligations doc row F3). CLIO's elicitation bridge correlates "
        "by protocol identity and the shared UserQuestion id only -- see "
        "gact/elicitation_bridge.py's module docstring."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
