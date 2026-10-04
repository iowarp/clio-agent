"""Rendered selection text -> markdown source span."""

from __future__ import annotations

import pytest

from clio_agent.gact.attention.rendered import find_rendered

SOURCE = (
    '## Result\n\nThe **closest** station is `MTA1` at [0.3 km](http://x.y/z "t").\n\n'
    "- east: 12\n- north: 3\n\n| a | b |\n|---|---|\n| 1 | 2 |"
)


@pytest.mark.parametrize(
    ("selected", "source_span"),
    [
        (
            "The closest station is MTA1 at 0.3 km.",
            'The **closest** station is `MTA1` at [0.3 km](http://x.y/z "t").',
        ),
        ("east: 12\nnorth: 3", "east: 12\n- north: 3"),
        ("Result", "Result"),
        ("a b", "a | b"),
    ],
)
def test_rendering_maps_back_to_the_exact_source_span(selected: str, source_span: str) -> None:
    span = find_rendered(SOURCE, selected)
    assert span is not None
    assert SOURCE[span[0] : span[1]] == source_span


def test_text_that_is_not_there_is_none() -> None:
    assert find_rendered(SOURCE, "west: 9") is None
    assert find_rendered(SOURCE, "   ") is None
