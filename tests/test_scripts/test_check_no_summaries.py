"""Tests for the baseline-0 no-summaries guard (iowarp/clio-agent#880, #832).

``scripts/check_no_summaries.py`` prevents the deleted server-authored delegation
*summary* layer from creeping back into ``src/clio_agent``. This pins the guard's
nontrivial logic — TOKEN-based scanning (``COMMENT`` tokens skipped), the banned
identifier + prose-matcher vocabulary, and the tokenize-failure line-scan fallback —
so a future edit cannot silently blind it (CLAUDE.md RULE 7: unit tests for new code).
"""

from __future__ import annotations

from pathlib import Path

from scripts.check_no_summaries import (
    BANNED_IDENTIFIERS,
    check_no_summaries,
    main,
    scan_source,
)


def test_scan_flags_reintroduced_dict_key() -> None:
    """A retired row / Part.metadata key reappearing as a string literal is caught."""
    hits = scan_source('row = {"output_summary": value}\n')
    assert hits == [(1, "output_summary")]


def test_scan_flags_attribute_and_identifier() -> None:
    """An attribute access and a ``def`` of a retired summarizer are both caught."""
    text = "x = pred.output_raw\n\ndef _compact_handoff_text():\n    return ''\n"
    lines = {line for line, _tok in scan_source(text)}
    tokens = {tok for _line, tok in scan_source(text)}
    assert 1 in lines and 3 in lines
    assert {"output_raw", "_compact_handoff_text"} <= tokens


def test_scan_flags_prose_matcher_regex_signature() -> None:
    """The DELETED prose matcher's ``typed\\s+workflow`` regex signature is caught."""
    hits = scan_source(r'PAT = re.compile(r"^.*typed\s+workflow state.*$")' + "\n")
    assert hits == [(1, r"typed\s+workflow")]


def test_scan_skips_comment_referencing_the_ban() -> None:
    """A COMMENT that names a banned token (e.g. the guard's own docs) does NOT flag."""
    text = "# #880: no output_summary here, and no output_raw either\nvalue = 1\n"
    assert scan_source(text) == []


def test_scan_does_not_flag_kept_public_prompt_cleaner_prose() -> None:
    """The KEPT ``workflow_state`` grounding vocabulary is deliberately NOT banned.

    The public-prompt cleaner scrubs clio's OWN injected context using
    ``workflow[_ ]state`` / ``Accumulated typed workflow state`` — none of which
    match the precise deleted-matcher signature ``typed\\s+workflow``.
    """
    text = (
        'label = "Accumulated typed workflow state"\npat = re.compile(r"\\bworkflow\\s+state\\b")\n'
    )
    assert scan_source(text) == []


def test_scan_fallback_scans_a_file_that_does_not_tokenize() -> None:
    """A syntactically broken file still cannot smuggle the vocabulary past the guard.

    ``tokenize`` raises on the unterminated string, so the conservative line-scan
    fallback runs and still catches the banned key on its code (non-comment) portion.
    """
    broken = 'def f(:\n    row = {"output_summary": "oops}\n'
    hits = scan_source(broken)
    assert any(tok == "output_summary" for _line, tok in hits)


def test_scan_fallback_still_skips_comment_portion() -> None:
    """The line-scan fallback strips the ``#`` comment tail before matching."""
    broken = "def f(:\n    x = 1  # output_raw mentioned only in a comment\n"
    assert scan_source(broken) == []


def test_scan_clean_source_has_no_hits() -> None:
    """Ordinary code with none of the vocabulary is clean."""
    assert scan_source("def add(a: int, b: int) -> int:\n    return a + b\n") == []


def test_all_banned_identifiers_are_individually_detected() -> None:
    """Every entry in the banned vocabulary is actually matched (no dead entries)."""
    for needle in BANNED_IDENTIFIERS:
        hits = scan_source(f"value = obj.{needle}\n")
        assert (1, needle) in hits, needle


def test_check_no_summaries_reports_violations_with_relative_path(tmp_path: Path) -> None:
    """The tree walker returns one Violation per hit with a forward-slash rel path."""
    src = tmp_path / "src" / "clio_agent"
    src.mkdir(parents=True)
    (src / "clean.py").write_text("def ok() -> int:\n    return 1\n", encoding="utf-8")
    (src / "offender.py").write_text('meta = {"output_summary": "x"}\n', encoding="utf-8")
    violations = check_no_summaries(src, rel_to=tmp_path)
    assert len(violations) == 1
    only = violations[0]
    assert only.rel == "src/clio_agent/offender.py"
    assert only.line == 1
    assert only.token == "output_summary"


def test_check_no_summaries_clean_tree_is_empty(tmp_path: Path) -> None:
    """A tree with no retired vocabulary yields no violations."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.py").write_text("value = compute()\n", encoding="utf-8")
    assert check_no_summaries(src) == []


def test_main_passes_on_the_real_source_tree() -> None:
    """The live ``src/clio_agent`` tree is clean at baseline 0 (the shipped invariant)."""
    assert main() == 0
