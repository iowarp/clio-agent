"""Tests for the baseline-0 settle/synthesis vocabulary guard (#948 S4 / #952).

``scripts/check_no_settle_vocabulary.py`` is the sole enforcement that the deleted
settle/synthesis routing vocabulary (typed routing fields, the settle loop, the
synthesis child, the inline delegate/fan-out tools) stays deleted. It is a
CI-blocking baseline-0 guard whose failure mode is inherently silent — a
``scan_source`` regression that returns ``[]`` keeps CI green forever because the
clean tree also returns ``[]`` — so these tests are the only detector of such a
regression (CLAUDE.md RULE 7: unit tests for new code). They pin the guard's
nontrivial logic (TOKEN-based scanning with ``COMMENT`` tokens skipped, the banned
vocabulary, and the tokenize-failure line-scan fallback), mirroring the sibling
``test_check_no_summaries.py``.
"""

from __future__ import annotations

from pathlib import Path

from scripts.check_no_settle_vocabulary import (
    BANNED_IDENTIFIERS,
    check_no_settle_vocabulary,
    main,
    scan_source,
)


def test_scan_flags_reintroduced_dict_key() -> None:
    """A retired routing field reappearing as a string literal (dict key) is caught."""
    hits = scan_source('row = {"next_expert": value}\n')
    assert hits == [(1, "next_expert")]


def test_scan_flags_attribute_and_def_identifier() -> None:
    """An attribute access and a ``def`` naming retired vocabulary are both caught."""
    text = (
        "x = pred.answer_stream_visible\n\ndef settle_dynamic_agent_delegations():\n    return ''\n"
    )
    lines = {line for line, _tok in scan_source(text)}
    tokens = {tok for _line, tok in scan_source(text)}
    assert 1 in lines and 3 in lines
    assert {"answer_stream_visible", "settle_dynamic"} <= tokens


def test_scan_flags_docstring_revival() -> None:
    """A docstring documenting a revived settle field is scanned (not a COMMENT token)."""
    text = 'def f() -> None:\n    """Emit next_task for the settle loop."""\n    return None\n'
    tokens = {tok for _line, tok in scan_source(text)}
    assert "next_task" in tokens


def test_scan_skips_comment_referencing_the_ban() -> None:
    """A COMMENT that names a banned token (e.g. this guard's own docs) does NOT flag."""
    text = "# #948: no next_expert here, and no final_responder either\nvalue = 1\n"
    assert scan_source(text) == []


def test_scan_does_not_flag_kept_settle_subsystems() -> None:
    """The kept ``settle_failed_finalize`` / ``settle_turn_transcript`` (#756 finalize
    error envelope) and the ARC handshake ``settle_s`` are NOT matched — only the
    precise ``settle_dynamic`` token is banned, never bare ``settle_``."""
    text = (
        "settle_failed_finalize(state)\nsettle_turn_transcript(state)\n"
        'init_settle_s = "handshake"\n'
    )
    assert scan_source(text) == []


def test_scan_fallback_scans_a_file_that_does_not_tokenize() -> None:
    """A syntactically broken file still cannot smuggle the vocabulary past the guard.

    ``tokenize`` raises on the unterminated string, so the conservative line-scan
    fallback runs and still catches the banned key on its code (non-comment) portion.
    """
    broken = 'def f(:\n    row = {"next_expert": "oops}\n'
    hits = scan_source(broken)
    assert any(tok == "next_expert" for _line, tok in hits)


def test_scan_fallback_still_skips_comment_portion() -> None:
    """The line-scan fallback strips the ``#`` comment tail before matching."""
    broken = "def f(:\n    x = 1  # next_expert mentioned only in a comment\n"
    assert scan_source(broken) == []


def test_scan_clean_source_has_no_hits() -> None:
    """Ordinary code with none of the vocabulary is clean."""
    assert scan_source("def add(a: int, b: int) -> int:\n    return a + b\n") == []


def test_all_banned_identifiers_are_individually_detected() -> None:
    """Every entry in the banned vocabulary is actually matched (no dead entries)."""
    for needle in BANNED_IDENTIFIERS:
        hits = scan_source(f'value = obj["{needle}"]\n')
        assert (1, needle) in hits, needle


def test_check_no_settle_vocabulary_reports_violations_with_relative_path(tmp_path: Path) -> None:
    """The tree walker returns one Violation per hit with a forward-slash rel path."""
    src = tmp_path / "src" / "clio_agent"
    src.mkdir(parents=True)
    (src / "clean.py").write_text("def ok() -> int:\n    return 1\n", encoding="utf-8")
    (src / "offender.py").write_text('cfg = {"max_sync_delegation_rounds": 3}\n', encoding="utf-8")
    violations = check_no_settle_vocabulary(src, rel_to=tmp_path)
    assert len(violations) == 1
    only = violations[0]
    assert only.rel == "src/clio_agent/offender.py"
    assert only.line == 1
    assert only.token == "max_sync_delegation_rounds"


def test_check_no_settle_vocabulary_clean_tree_is_empty(tmp_path: Path) -> None:
    """A tree with no retired vocabulary yields no violations."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.py").write_text("value = compute()\n", encoding="utf-8")
    assert check_no_settle_vocabulary(src) == []


def test_main_passes_on_the_real_source_tree() -> None:
    """The live ``src/clio_agent`` tree is clean at baseline 0 (the shipped invariant)."""
    assert main() == 0
