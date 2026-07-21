#!/usr/bin/env python3
"""Baseline-0 guard: no settle/synthesis routing vocabulary in the source tree (#948, #952).

Campaign #948 S4 deleted the settle/synthesis orchestration layer wholesale: a
tier-1 main is a react agent whose ``answer`` IS the user deliverable, and it
routes by SPAWNING declared children as real child turns (the spawn runtime in
``gact/agents/spawn_runtime.py``). No typed routing field, no settle loop, no
synthesis child, no inline per-child delegate/fan-out tools. This guard prevents
the deleted vocabulary from creeping back in. It walks ``src/clio_agent/**/*.py``
and fails (baseline 0) if any retired token reappears in CODE:

* ``next_expert`` / ``next_task`` — the typed routing fields a main emitted for
  the settle loop to consume (deleted with the loop; mains now call spawn tools).
* ``final_responder`` — the synthesis-child adoption flag and its degradation
  vocabulary (mains answer directly; adoption machinery deleted).
* ``settle_dynamic`` — ``settle_dynamic_agent_delegations`` and any settle-loop
  revival. NOTE: ``settle_failed_finalize`` / ``settle_turn_transcript`` (the
  #756 finalize error envelope) are a DIFFERENT, kept subsystem and are not
  matched by this token.
* ``_dynamic_parent_resume_prompt`` — the synthesized parent re-invoke prompt
  (parents stay resident; nothing is faked back via prompt blocks).
* ``delegate_to_`` / ``fanout_to_children`` — the inline in-thread child tools
  (replaced by ``spawn_agent_task`` / ``wait_agent_tasks`` /
  ``spawn_agents_parallel`` over real child sessions).
* ``max_sync_delegation_rounds`` — the settle-loop round budget.
* ``answer_stream_visible`` — the main-answer suppression flag (the main's
  answer is the deliverable; it is never hidden).
* ``migration_signals`` — the deleted ``agents/migration_signals.py`` seam.

MATCHING RULES (same contract as ``check_no_summaries.py``):

* The scan is TOKEN-based (:mod:`tokenize`), NOT a raw substring grep. ``COMMENT``
  tokens are SKIPPED; identifiers, dict/string keys, and docstrings ARE scanned,
  so a reintroduced field, function, tool name, or documented revival is caught.
* The guard scans ONLY ``src/clio_agent``; this file lives in ``scripts/`` and
  lists the banned tokens itself, so it is never self-flagged.
* A file that fails to tokenize is scanned line-by-line (comment-stripped) so a
  broken file cannot smuggle vocabulary past the guard.

Run as part of CI (blocking) and locally::

    uv run python scripts/check_no_settle_vocabulary.py
"""

from __future__ import annotations

import io
import sys
import tokenize
from pathlib import Path
from typing import NamedTuple

# Root of the source tree to scan, relative to the repository root.
SRC_ROOT = "src/clio_agent"

# Retired identifiers / keys: a non-comment token containing any of these (as a
# substring) is a violation. This is the settle/synthesis routing vocabulary
# #948 S4 deleted.
BANNED_IDENTIFIERS: tuple[str, ...] = (
    "next_expert",
    "next_task",
    "final_responder",
    "settle_dynamic",
    "_dynamic_parent_resume_prompt",
    "delegate_to_",
    "fanout_to_children",
    "max_sync_delegation_rounds",
    "answer_stream_visible",
    "migration_signals",
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

    ``COMMENT`` tokens are skipped so a comment referencing the ban does not
    false-positive; every other token (identifiers, string literals, docstrings)
    is checked. A file that does not tokenize is scanned line-by-line as a
    conservative fallback.
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


def check_no_settle_vocabulary(scan_root: Path, *, rel_to: Path | None = None) -> list[Violation]:
    """Evaluate the baseline-0 settle-vocabulary guard under ``scan_root``.

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
    violations = check_no_settle_vocabulary(repo_root / SRC_ROOT, rel_to=repo_root)
    if not violations:
        print(
            f"OK: no settle/synthesis routing vocabulary under {SRC_ROOT} (baseline 0, #948 S4)."
        )
        return 0
    print(f"FAIL: {len(violations)} settle/synthesis vocabulary hit(s) (#948 S4):")
    for entry in violations:
        print(f"  {entry.rel}:{entry.line} reintroduced banned token {entry.token!r}")
    print(
        "Tier-1 mains are react agents that answer directly and route by spawning "
        "real child turns (spawn_agent_task / wait_agent_tasks / spawn_agents_parallel). "
        "The settle loop, synthesis children, typed routing fields, and inline child "
        "tools are deleted and must not return. See issues #948 / #952."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
