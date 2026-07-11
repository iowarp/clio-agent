#!/usr/bin/env python3
"""Baseline-0 guard: no delegation-summary vocabulary in the source tree (#880, #832).

Issue #880 ripped out the entire server-authored delegation *summary* layer: a
completed delegation's ``output`` is the child's answer BYTE-FOR-BYTE, never a
server-synthesized one-liner, and no code path may author text into a field the
UI renders as the child's answer. This guard prevents the layer from creeping
back in. It walks ``src/clio_agent/**/*.py`` and fails (baseline 0) if any of the
retired vocabulary reappears in CODE:

* the retired row / Part.metadata keys and their producers::

      output_summary   output_raw
      public_return_summary            (the deleted return_summary.py seam)
      _expert_result_summary           (the deleted Tier-1 summarizer)
      _failed_child_delegation_output_summary
      _compact_handoff_text

* the deleted natural-language prose matcher that stripped any sentence
  containing the phrase *typed workflow state* from the model's answer -- its
  regex signature ``typed\\s+workflow`` (superseding principle #1: clio core must
  not decide visibility of model prose by keyword matching).

* the visible-text prose cleaners #881 deleted -- the whole scrub seam that
  edited a model's visible thought/answer prose against a declared vocabulary::

      _clean_public_transcript_text   (the transcript visible-text scrubber)
      _clean_public_delegation_prompt (the public-call-prompt scrubber)
      _scrub_alternation              (their shared alias-alternation builder)

  Epic #880/#881: the client renders model prose VERBATIM and the server fixes
  leaks at the root (the #877 line-start marker split), so no core code may
  scrub a model's visible text. The kept ``_delegated_expert_public_prompt``
  splits a SERVER-COMPOSED prompt at the server's OWN marker constants (structural
  string handling), which is not a prose matcher and is not banned.

MATCHING RULES (documented per #880 hazard 8):

* The scan is TOKEN-based (:mod:`tokenize`), NOT a raw substring grep. ``COMMENT``
  tokens are SKIPPED, so a comment that references the ban itself (e.g.
  ``# #880: no output_summary``) does NOT false-positive. Every other token --
  identifiers (``row.output_summary``), string keys (``"output_summary": ...``),
  and regex string literals (``r"...typed\\s+workflow..."``) -- IS scanned, so a
  reintroduced dict key, attribute, function, or prose matcher is caught.
* The guard scans ONLY ``src/clio_agent`` (:data:`SRC_ROOT`); this file lives in
  ``scripts/`` and lists the banned tokens in its own vocabulary, so it is never
  self-flagged.
* ``typed\\s+workflow`` is the precise signature of the DELETED return-path prose
  matcher. The literal token ``workflow_state`` itself is NOT banned: it is the
  first-class typed structured field on the delegation return contract and the key
  of the server-composed grounding block, both of which core legitimately reads
  and writes. The guard bans only the prose-scrubbing FUNCTIONS (above) and the
  ``typed\\s+workflow`` heuristic signature, never the structured field name.

Run as part of CI (blocking) and locally::

    uv run python scripts/check_no_summaries.py
"""

from __future__ import annotations

import io
import sys
import tokenize
from pathlib import Path
from typing import NamedTuple

# Root of the source tree to scan, relative to the repository root.
SRC_ROOT = "src/clio_agent"

# Retired identifiers / keys: a token containing any of these (as a substring, so
# ``row["output_summary"]`` and ``def _compact_handoff_text`` both match) is a
# violation. These are the delegation-summary vocabulary #880 deleted.
BANNED_IDENTIFIERS: tuple[str, ...] = (
    "output_summary",
    "output_raw",
    "public_return_summary",
    "_expert_result_summary",
    "_failed_child_delegation_output_summary",
    "_compact_handoff_text",
    # #881: the visible-text prose cleaners — the scrub seam that edited a model's
    # visible thought/answer/prompt prose against a declared vocabulary. Deleted;
    # must not grow back (the client renders verbatim, the root is fixed at #877).
    "_clean_public_transcript_text",
    "_clean_public_delegation_prompt",
    "_scrub_alternation",
)

# Regex-signature of the DELETED prose matcher (the ``typed workflow state``
# sentence remover). The literal ``\s+`` (backslash-s-plus) only ever appears
# inside a regex string, never in prose, so this cannot match a grounding comment
# or the kept public-prompt cleaner's ``workflow[_ ]state`` patterns.
BANNED_PROSE_MATCHER = r"typed\s+workflow"


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

    ``COMMENT`` tokens are skipped so a comment referencing the ban does not
    false-positive; every other token (identifiers, string literals) is checked.
    A file that does not tokenize (syntax error) is scanned line-by-line as a
    conservative fallback so a broken file can never smuggle the vocabulary past
    the guard.
    """

    banned = (*BANNED_IDENTIFIERS, BANNED_PROSE_MATCHER)
    hits: list[tuple[int, str]] = []
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(text).readline))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        for lineno, raw in enumerate(text.splitlines(), start=1):
            code = raw.split("#", 1)[0]
            for needle in banned:
                if needle in code:
                    hits.append((lineno, needle))
        return hits
    for tok in tokens:
        if tok.type == tokenize.COMMENT:
            continue
        for needle in banned:
            if needle in tok.string:
                hits.append((tok.start[0], needle))
    return hits


def check_no_summaries(scan_root: Path, *, rel_to: Path | None = None) -> list[Violation]:
    """Evaluate the baseline-0 no-summaries guard under ``scan_root``.

    Args:
        scan_root: Directory tree to walk for ``*.py`` files.
        rel_to: Base directory for the reported forward-slash relative path.

    Returns:
        Every :class:`Violation` found (empty when the tree is clean).
    """

    base = rel_to if rel_to is not None else scan_root
    violations: list[Violation] = []
    for path in sorted(scan_root.rglob("*.py")):
        rel = path.relative_to(base).as_posix()
        text = path.read_text(encoding="utf-8", errors="replace")
        for line, token in scan_source(text):
            violations.append(Violation(rel, line, token))
    return violations


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Return 0 when the tree is clean, 1 on any violation."""

    repo_root = _repo_root()
    violations = check_no_summaries(repo_root / SRC_ROOT, rel_to=repo_root)
    if not violations:
        print(f"OK: no delegation-summary vocabulary under {SRC_ROOT} (baseline 0, #880).")
        return 0
    print(f"FAIL: {len(violations)} delegation-summary vocabulary hit(s) (#880):")
    for entry in violations:
        print(f"  {entry.rel}:{entry.line} reintroduced banned token {entry.token!r}")
    print(
        "The delegation return contract is { output , workflow_state }; ``output`` "
        "is the child's answer verbatim. No server-authored summary, no output_raw, "
        "no prose matcher on model output. See issue #880."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
