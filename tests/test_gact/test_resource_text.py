"""Paged reads and the streaming search reach every character of a resource's text."""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any

import pytest

from clio_agent.gact import resource_text
from clio_agent.gact.resource_text import read_text_page, search_text


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "text.txt"
    path.write_bytes(text.encode("utf-8"))
    return path


@pytest.mark.parametrize("count_total", [True, False])
def test_pages_concatenate_to_the_whole_text(tmp_path: Path, count_total: bool) -> None:
    text = "Name,Value\r\n" + "café,地球\n" * 500
    path = _write(tmp_path, text)
    pages, offset = [], 0
    for _ in range(len(text) // 97 + 2):  # bounded: a runaway pager fails, never hangs
        page = read_text_page(path, offset=offset, limit=97, count_total=count_total)
        pages.append(page.content)
        if page.next_offset is None:
            assert page.total_chars == len(text)
            break
        assert page.total_chars == (len(text) if count_total else None)
        offset = page.next_offset
    else:
        raise AssertionError("the pages never reached the end")
    assert "".join(pages) == text


def test_a_page_past_the_end_is_empty_and_reports_the_length(tmp_path: Path) -> None:
    page = read_text_page(_write(tmp_path, "short"), offset=50, limit=10, count_total=False)
    assert page == resource_text.TextPage(content="", next_offset=None, total_chars=5)


def test_skipping_streams_in_pieces(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(resource_text, "PIECE_CHARS", 7)
    text = "".join(chr(ord("a") + n % 26) for n in range(1000))
    page = read_text_page(_write(tmp_path, text), offset=500, limit=20, count_total=True)
    assert page.content == text[500:520] and page.next_offset == 520
    assert page.total_chars == 1000


def test_search_pages_cover_every_match_and_offsets_point_into_the_text(tmp_path: Path) -> None:
    lines = [f"row {n} {'HIT' if n % 3 == 0 else 'miss'}" for n in range(300)]
    text = "\r\n".join(lines) + "\n"
    path = _write(tmp_path, text)
    found: list[dict[str, Any]] = []
    offset: int | None = 0
    for _ in range(100 // 7 + 2):  # bounded: a runaway pager fails, never hangs
        if offset is None:
            break
        matches, offset = search_text(path, "hit", offset=offset, limit=7, excerpt_chars=500)
        assert len(matches) <= 7
        found.extend(matches)
    assert [match["line"] for match in found] == [n + 1 for n in range(300) if n % 3 == 0]
    for match in found:
        start = match["char_offset"]
        assert text[start : start + len(match["text"])] == match["text"]
        assert "line_chars" not in match


def test_a_line_without_newlines_is_searched_in_pieces_across_boundaries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(resource_text, "PIECE_CHARS", 16)
    text = "x" * 1000 + "NEEDLE" + "y" * 1000
    path = _write(tmp_path, text)
    for shift in range(16):  # the needle crosses every possible piece boundary
        shifted = "z" * shift + text
        path.write_text(shifted, encoding="utf-8")
        matches, more = search_text(path, "needle", offset=0, limit=5, excerpt_chars=40)
        assert more is None and len(matches) == 1
        match = matches[0]
        assert "NEEDLE" in match["text"] and match["line_chars"] == len(shifted)
        start = match["char_offset"]
        assert shifted[start : start + len(match["text"])] == match["text"]


def test_a_long_line_excerpt_is_centred_on_its_hit(tmp_path: Path) -> None:
    text = "a" * 5000 + "target" + "b" * 5000 + "\nnext line\n"
    matches, _ = search_text(_write(tmp_path, text), "target", offset=0, limit=5, excerpt_chars=100)
    assert matches[0]["text"].startswith("a" * 25 + "target")
    assert matches[0]["line_chars"] == 10006 and matches[0]["char_offset"] == 4975


def test_skipping_never_decodes_more_than_a_piece_at_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Skipping to a far offset (or to the end) holds one piece at a time, not the file."""

    class _Spy(io.StringIO):
        sizes: list[int | None] = []

        def read(self, size: int | None = -1, /) -> str:
            self.sizes.append(size)
            return super().read(size)

    monkeypatch.setattr(resource_text, "PIECE_CHARS", 10)
    stream = _Spy("x" * 95)
    assert resource_text._skip(stream, 60) == 60
    assert resource_text._skip(stream, None) == 35
    assert all(size is not None and 0 < size <= 10 for size in _Spy.sizes)
