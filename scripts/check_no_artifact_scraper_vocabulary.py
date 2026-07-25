#!/usr/bin/env python3
"""Baseline-0 guard: no retired artifact path-string vocabulary in the tree (#966 S7 / #973).

The artifacts campaign made the **registry** the source of truth and DELETED the
path-string mechanisms it replaced (owner decision #966, deletion items 4/5/6):

* the inert ``structured_outputs.artifacts`` field (S3 #969) — the model designates
  a report with the ``create_artifact`` tool, never a passthrough field;
* the ``evidence.py`` answer-grounding heuristics (S7 #973 item 4) —
  ``_ground_fabricated_local_artifact_paths`` / ``_verified_local_artifact_paths_by_ext``
  / ``_is_remote_artifact_ref``, replaced by registry-sourced
  ``ground_answer_artifacts``;
* the test-harness / benchmark artifact scrapers (S7 #973 item 5) —
  ``_artifact_paths`` / ``_visualization_artifact_paths`` / ``_ARTIFACT_PATH_RE`` /
  ``_is_valid_artifact_path_candidate`` / ``_is_staged_metadata_input_path``, replaced
  by ``_registry_artifact_paths`` (a query of ``GET /v1/sessions/{sid}/artifacts``).

This guard walks ``src/clio_agent``, ``tests``, and ``scripts`` and fails
(baseline 0) if any retired symbol reappears in CODE. It follows the
``check_no_settle_vocabulary.py`` contract with one deliberate difference:

* matching is by EXACT ``NAME`` token (not substring), so the surviving
  ``_registry_artifact_paths`` / ``ground_answer_artifacts`` / ``visualization_artifacts``
  replacements are never false-flagged;
* ``COMMENT`` and ``STRING`` tokens are SKIPPED, so a docstring/comment that
  *documents* the deletion (this file, the design doc, the module docstrings) is not
  self-flagged — only a live identifier usage is a violation;
* ``structured_outputs.artifacts`` is caught as the NAME/OP/NAME token sequence
  ``structured_outputs`` ``.`` ``artifacts`` (a bare ``.artifacts`` access such as
  ``result.artifacts`` is legitimate and NOT matched).

Run as part of CI (blocking) and locally::

    uv run python scripts/check_no_artifact_scraper_vocabulary.py
"""

from __future__ import annotations

import io
import sys
import tokenize
from pathlib import Path
from typing import NamedTuple

#: Source trees to scan, relative to the repository root.
SCAN_ROOTS = ("src/clio_agent", "tests", "scripts")

#: Retired identifiers — an EXACT ``NAME`` token equal to any of these is a violation.
BANNED_NAMES: frozenset[str] = frozenset(
    {
        # evidence.py answer-grounding heuristics (S7 item 4)
        "_ground_fabricated_local_artifact_paths",
        "_verified_local_artifact_paths_by_ext",
        "_is_remote_artifact_ref",
        # test-harness / benchmark scrapers (S7 item 5)
        "_artifact_paths",
        "_visualization_artifact_paths",
        "_ARTIFACT_PATH_RE",
        "_is_valid_artifact_path_candidate",
        "_is_staged_metadata_input_path",
    }
)

#: This file itself lists the banned tokens (in strings/comments) — never self-flag.
_SELF = "check_no_artifact_scraper_vocabulary.py"


class Violation(NamedTuple):
    """A retired token found in a scanned source file."""

    rel: str
    line: int
    token: str


def _repo_root() -> Path:
    """Return the repository root (parent of the ``scripts`` directory)."""
    return Path(__file__).resolve().parent.parent


def scan_source(text: str) -> list[tuple[int, str]]:
    """Return ``(line, matched_token)`` for every retired symbol in CODE.

    ``COMMENT`` and ``STRING`` tokens are skipped (documentation of the deletion is
    not a violation). Matching is by exact ``NAME`` token, plus the
    ``structured_outputs.artifacts`` NAME/OP/NAME sequence. A file that does not
    tokenize falls back to a conservative word-boundary line scan.
    """
    hits: list[tuple[int, str]] = []
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(text).readline))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        import re

        for lineno, raw in enumerate(text.splitlines(), start=1):
            code = raw.split("#", 1)[0]
            for name in BANNED_NAMES:
                if re.search(rf"\b{re.escape(name)}\b", code):
                    hits.append((lineno, name))
            if re.search(r"\bstructured_outputs\s*\.\s*artifacts\b", code):
                hits.append((lineno, "structured_outputs.artifacts"))
        return hits

    prev2: str = ""
    prev1: str = ""
    for tok in tokens:
        if tok.type in (tokenize.COMMENT, tokenize.STRING):
            prev2, prev1 = prev1, ""
            continue
        if tok.type == tokenize.NAME:
            if tok.string in BANNED_NAMES:
                hits.append((tok.start[0], tok.string))
            # structured_outputs . artifacts  (NAME OP NAME)
            if tok.string == "artifacts" and prev1 == "." and prev2 == "structured_outputs":
                hits.append((tok.start[0], "structured_outputs.artifacts"))
            prev2, prev1 = prev1, tok.string
        elif tok.type == tokenize.OP:
            prev2, prev1 = prev1, tok.string
        elif tok.type in (tokenize.NL, tokenize.NEWLINE, tokenize.INDENT, tokenize.DEDENT):
            continue
        else:
            prev2, prev1 = prev1, ""
    return hits


def check_no_artifact_scraper_vocabulary(
    repo_root: Path,
) -> list[Violation]:
    """Evaluate the baseline-0 guard across :data:`SCAN_ROOTS` under ``repo_root``."""
    violations: list[Violation] = []
    for root in SCAN_ROOTS:
        scan_root = repo_root / root
        if not scan_root.is_dir():
            continue
        for path in sorted(scan_root.rglob("*.py")):
            if path.name == _SELF:
                continue
            rel = path.relative_to(repo_root).as_posix()
            text = path.read_text(encoding="utf-8", errors="replace")
            for line, token in scan_source(text):
                violations.append(Violation(rel, line, token))
    return violations


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Return 0 when the tree is clean, 1 on any violation."""
    repo_root = _repo_root()
    violations = check_no_artifact_scraper_vocabulary(repo_root)
    if not violations:
        print(
            "OK: no retired artifact path-string vocabulary under "
            f"{', '.join(SCAN_ROOTS)} (baseline 0, #966 S7 / #973)."
        )
        return 0
    print(f"FAIL: {len(violations)} retired artifact-scraper vocabulary hit(s) (#973):")
    for entry in violations:
        print(f"  {entry.rel}:{entry.line} reintroduced retired token {entry.token!r}")
    print(
        "The artifact REGISTRY is the source of truth: designate artifacts (tool "
        "output-path args / create_artifact / pin), ground answers via "
        "ground_answer_artifacts, and query GET /v1/sessions/{sid}/artifacts "
        "(_registry_artifact_paths) — never a path-string scrape of tool prose or "
        "workflow_state. See issues #966 / #973."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
