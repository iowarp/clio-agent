"""Preserve exact transcript coordinates when a template strips boundary whitespace."""

from __future__ import annotations

from clio_agent.gact.attention.transcript_map import transcript_locations


def test_boundary_whitespace_keeps_source_coordinates() -> None:
    """Only displayed content receives runs; leading and trailing whitespace do not."""
    [hit] = transcript_locations("prefix abc suffix", " \nabc\n")
    assert hit.index == [7, 7, 7, 8, 9, 10, 10]
    assert hit.from_haystack(7, 10) == (2, 5)
    assert hit.to_haystack(2, 5) == (7, 10)


def test_exact_matches_win_and_repeated_content_stays_ambiguous() -> None:
    """An exact captured newline is retained; a trim never makes repeated text unique."""
    [hit] = transcript_locations("x abc\n y", "abc\n")
    assert hit.encoding == "verbatim"
    assert len(transcript_locations("abc and abc", "abc\n")) == 2
    assert transcript_locations("ab c", "abc\n") == []
    assert transcript_locations("abc", "\n \t") == []


def test_json_escaped_content_retains_original_unicode_offsets() -> None:
    """The existing JSON transform composes with exterior trimming without fuzzy matching."""
    [hit] = transcript_locations("prefix a\\nb suffix", " a\nb\n")
    assert hit.encoding == "json+boundary-trim"
    assert hit.from_haystack(7, 11) == (1, 4)
