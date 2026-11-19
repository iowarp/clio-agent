"""Datasets with long catalog titles can still be attached."""

from __future__ import annotations

from clio_agent.gact.storage.models import CreateSource
from clio_agent.gact.storage.ndp_selection import MAX_LABEL, source_label

LONG = (
    "Dissolved mercury (Hg) speciation in the California Current System from samples "
    "collected on R/V Roger Revelle cruise RR2105 in July and August 2021"
)


def test_long_titles_are_shortened_at_a_word_and_validate() -> None:
    label = source_label(LONG)
    assert len(label) <= MAX_LABEL and label.endswith("…")
    assert LONG.startswith(label[:-1])
    CreateSource(provider="ndp", root="d", label=label, mode="read_only")


def test_short_titles_are_kept_and_blank_ones_named() -> None:
    assert source_label("Lake  Tahoe\nBasin Lidar") == "Lake Tahoe Basin Lidar"
    assert source_label("   ") == "NDP dataset"
