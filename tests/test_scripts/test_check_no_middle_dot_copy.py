"""Tests for the baseline-0 middle-dot guard on server copy.

The guard's failure mode is silent: a ``scan_source`` regression that returns
``[]`` keeps CI green because the clean tree also returns ``[]``. These tests pin
what it must catch (plain, f-string and escaped literals) and what it must skip
(comments, docstrings, allow-marked lines).
"""

from __future__ import annotations

from pathlib import Path

from scripts.check_no_middle_dot_copy import check_no_middle_dot_copy, main, scan_source


def test_scan_flags_plain_and_fstring_literals() -> None:
    """A dot in a plain literal and in an f-string piece are both caught."""
    text = 'a = "x · y"\nb = f"{a} · partial"\n'
    assert [line for line, _ in scan_source(text)] == [1, 2]


def test_scan_flags_escaped_spellings() -> None:
    """The escaped forms resolve to the same character and are caught."""
    text = 'a = "x \\u00b7 y"\nb = "x \\N{MIDDLE DOT} y"\n'
    assert [line for line, _ in scan_source(text)] == [1, 2]


def test_scan_skips_comments_and_docstrings() -> None:
    """Comments and docstrings never reach the UI and may quote upstream text."""
    text = (
        '"""Module · doc."""\n'
        "# comment · here\n"
        "def f():\n"
        '    """Upstream says "Not logged in · Please run /login"."""\n'
        "    return 1\n"
    )
    assert scan_source(text) == []


def test_scan_honours_allow_marker() -> None:
    """A literal that must match third-party output can opt out on its line."""
    text = 'a = "x · y"  # allow-middle-dot: matches the CLI output\n'
    assert scan_source(text) == []


def test_scan_reports_unparseable_file_by_line() -> None:
    """A syntax error cannot hide a violation."""
    text = 'a = "x · y"\ndef broken(:\n'
    assert [line for line, _ in scan_source(text)] == [1]


def test_check_reports_relative_paths(tmp_path: Path) -> None:
    """Violations carry the forward-slash path relative to ``rel_to``."""
    pkg = tmp_path / "src" / "pkg"
    pkg.mkdir(parents=True)
    (pkg / "clean.py").write_text('a = "x, y"\n', encoding="utf-8")
    (pkg / "dirty.py").write_text('a = "x · y"\n', encoding="utf-8")
    violations = check_no_middle_dot_copy(tmp_path / "src", rel_to=tmp_path)
    assert [(v.rel, v.line) for v in violations] == [("src/pkg/dirty.py", 1)]


def test_real_tree_is_clean() -> None:
    """The shipped server copy has no middle dot."""
    assert main([]) == 0
