"""Frontmatter parsing is done once per distinct text.

Measured live (2026-09-30): every turn re-parsed the YAML frontmatter of every
installed blueprint, expert and pack -- ~1.3 s of a follow-up turn's prologue, all
before the first model call. The parse is a pure function of the text, so it is
memoized on the text; callers get their own copy, so one caller's change can never
reach another.
"""

from __future__ import annotations

import time

from clio_agent.gact import frontmatter

TEXT = "---\nid: pack\ntools:\n  - a\n  - b\nnested:\n  key: [1, 2]\n---\nBody text.\n"


def test_the_same_text_parses_once(monkeypatch) -> None:
    calls = []
    real = frontmatter._parse_frontmatter_uncached

    def counting(text: str):
        calls.append(text)
        return real(text)

    monkeypatch.setattr(frontmatter, "_parse_frontmatter_uncached", counting)
    frontmatter._FRONTMATTER_MEMO.clear()

    first = frontmatter._parse_frontmatter(TEXT)
    second = frontmatter._parse_frontmatter(TEXT)

    assert (
        first
        == second
        == ({"id": "pack", "tools": ["a", "b"], "nested": {"key": [1, 2]}}, "Body text.")
    )
    assert len(calls) == 1


def test_a_caller_cannot_change_what_the_next_caller_gets() -> None:
    frontmatter._FRONTMATTER_MEMO.clear()
    meta, _ = frontmatter._parse_frontmatter(TEXT)
    meta["tools"].append("mutated")
    meta["nested"]["key"].clear()

    again, _ = frontmatter._parse_frontmatter(TEXT)

    assert again == {"id": "pack", "tools": ["a", "b"], "nested": {"key": [1, 2]}}


def test_changed_text_is_parsed_fresh() -> None:
    frontmatter._FRONTMATTER_MEMO.clear()
    frontmatter._parse_frontmatter(TEXT)
    changed = TEXT.replace("id: pack", "id: other")
    assert frontmatter._parse_frontmatter(changed)[0]["id"] == "other"


def test_a_repeated_parse_is_much_cheaper() -> None:
    frontmatter._FRONTMATTER_MEMO.clear()
    big = "---\n" + "\n".join(f"key{i}: value {i}" for i in range(400)) + "\n---\nbody"
    started = time.perf_counter()
    frontmatter._parse_frontmatter(big)
    cold = time.perf_counter() - started
    started = time.perf_counter()
    for _ in range(10):
        frontmatter._parse_frontmatter(big)
    warm = (time.perf_counter() - started) / 10
    assert warm < cold / 3
