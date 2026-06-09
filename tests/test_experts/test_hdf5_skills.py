"""Tests for the bundled HDF5 skill loader.

These tests guard against (a) accidental deletions of bundled skills,
(b) regressions in the frontmatter parser, and (c) recall failures in
match_skills.
"""

from __future__ import annotations

import pytest

from clio_agent.experts.hdf5_skills import (
    SkillNotFoundError,
    list_skills,
    load_skill,
    match_skills,
    skill_names,
)

EXPECTED_SKILL_COUNT = 24


def test_skill_count_matches_origin():
    assert len(skill_names()) == EXPECTED_SKILL_COUNT


def test_every_skill_has_a_description():
    for summary in list_skills():
        assert summary["name"]
        assert summary["description"], f"empty description for {summary['name']}"


def test_load_skill_returns_full_body():
    body = load_skill("hdf5-chunking")
    assert body.startswith("---")
    assert "name: hdf5-chunking" in body
    assert "Chunk Size Guidelines" in body or "chunk" in body.lower()


def test_load_skill_rejects_unknown_name():
    with pytest.raises(SkillNotFoundError):
        load_skill("hdf5-does-not-exist")


def test_load_skill_rejects_path_traversal():
    with pytest.raises(SkillNotFoundError):
        load_skill("../../../etc/passwd")
    with pytest.raises(SkillNotFoundError):
        load_skill("hdf5-chunking/../hdf5-filters")


@pytest.mark.parametrize(
    "query, expected_top",
    [
        ("I want to rechunk my dataset", "hdf5-chunking"),
        ("how do I compress with gzip", "hdf5-filters"),
        ("MPI-IO collective writes", "hdf5-parallel"),
        ("read HDF5 from S3 using ros3", "hdf5-ros3-vfd"),
        ("SWMR single writer multiple reader", "hdf5-swmr"),
        ("create a virtual dataset combining files", "hdf5-vds"),
        ("publish my HDF5 to Zenodo with a DOI", "hdf5-scientific-publishing"),
        ("compound datatype with variable-length strings", "hdf5-datatypes"),
        ("plot temperature dataset as heatmap", "hdf5-visualization"),
        ("check if my NetCDF4 file is CF compliant", "hdf5-cf-compliance"),
    ],
)
def test_match_skills_recall(query, expected_top):
    """Each canonical phrase should surface its intended skill in the top-3."""
    top = [name for name, _ in match_skills(query, top_k=3)]
    assert expected_top in top, f"query={query!r} returned {top}, missing {expected_top}"


def test_match_skills_returns_empty_for_unrelated_query():
    assert match_skills("the weather today is nice") == []
