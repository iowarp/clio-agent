"""Long-term memory documents round-trip byte for byte and refuse anything else."""

from __future__ import annotations

import pytest

from clio_agent.arc import memory_document as md

PLAN_EXAMPLE = """\
---
name: frontier-allocation
description: The user's OLCF Frontier allocation, project id and scheduler quirks
category: environments
aliases: [frontier, olcf]
updated: 2026-11-04T15:02:11Z
---
- Project id is CSC123; jobs go to the batch partition by default {id=f3k9, source=user, session=sess_ab12, at=2026-10-30}
- Prefers 2-hour walltime requests for test runs {id=f3m1, source=inferred, session=sess_cd34, at=2026-11-04}
- ~~Uses the debug QOS for short tests~~ {id=f2a0, source=user, session=sess_ef56, at=2026-09-12, superseded_by=f3m1}
"""  # noqa: E501 - a document line is as long as its fact


def test_the_plan_example_round_trips_byte_for_byte() -> None:
    document = md.parse(PLAN_EXAMPLE)
    assert md.serialize(document) == PLAN_EXAMPLE
    assert [fact.id for fact in document.live_facts] == ["f3k9", "f3m1"]
    assert document.fact("f2a0").superseded_by == "f3m1"
    assert document.aliases == ("frontier", "olcf")


def test_search_text_holds_live_facts_but_not_superseded_ones() -> None:
    text = md.search_text(md.parse(PLAN_EXAMPLE))
    assert "CSC123" in text and "walltime" in text and "olcf" in text
    assert "debug QOS" not in text


def test_a_profile_carries_its_forgotten_list() -> None:
    profile = md.MemoryDocument(
        name="profile",
        description="Who the user is",
        category="profile",
        updated="2026-10-23T00:00:00Z",
        forgotten=("f1aaaa", "f2bbbb"),
    )
    text = md.serialize(profile)
    assert "forgotten: [f1aaaa, f2bbbb]" in text
    assert md.parse(text) == profile
    again = md.forgetting(profile, ("f2bbbb", "f3cccc"))
    assert again.forgotten == ("f1aaaa", "f2bbbb", "f3cccc")


def test_supersede_strikes_the_old_fact_and_forget_deletes_the_line() -> None:
    document = md.parse(PLAN_EXAMPLE)
    replacement = md.Fact(
        id="f9zzzz", text="Project id is CSC456", source="user", session="sess_x", at="2026-11-05"
    )
    updated = md.superseding(document, "f3k9", replacement, updated="2026-11-05T00:00:00Z")
    text = md.serialize(updated)
    assert "~~Project id is CSC123; jobs go to the batch partition by default~~" in text
    assert "superseded_by=f9zzzz" in text
    assert md.parse(text) == updated
    with pytest.raises(md.MemoryDocumentError):
        md.superseding(updated, "f3k9", replacement, updated="x")  # no longer live

    forgotten = md.without_fact(updated, "f9zzzz", updated="2026-11-06T00:00:00Z")
    assert "CSC456" not in md.serialize(forgotten)


@pytest.mark.parametrize(
    "text",
    [
        PLAN_EXAMPLE.replace("name: frontier", "name: Frontier"),  # not a slug
        PLAN_EXAMPLE.replace("category: environments", "category: hobbies"),
        PLAN_EXAMPLE.replace("source=user, session=sess_ab12", "source=web, session=sess_ab12"),
        PLAN_EXAMPLE.replace("aliases: [frontier, olcf]", "aliases: [frontier,olcf]"),
        PLAN_EXAMPLE.replace("updated: 2026", "updated:  2026"),
        PLAN_EXAMPLE.replace("{id=f3k9, source=user", "{source=user, id=f3k9"),
        PLAN_EXAMPLE.replace("~~Uses the debug QOS for short tests~~", "Uses the debug QOS"),
        PLAN_EXAMPLE.rstrip("\n"),
        PLAN_EXAMPLE.replace("\n", "\r\n"),
        PLAN_EXAMPLE.replace("id=f3m1, source=inferred", "id=f3k9, source=inferred"),
        "no frontmatter\n",
    ],
)
def test_non_canonical_text_is_refused_with_a_typed_error(text: str) -> None:
    with pytest.raises(md.MemoryDocumentError) as caught:
        md.parse(text)
    assert caught.value.error_type == "memory_document_invalid"


def test_new_fact_ids_are_valid_and_avoid_taken_ones() -> None:
    taken = {md.new_fact_id() for _ in range(50)}
    fresh = md.new_fact_id(taken)
    assert fresh not in taken
    md.serialize(
        md.MemoryDocument(
            name="x",
            description="d",
            category="topics",
            updated="u",
            facts=(md.Fact(id=fresh, text="t", source="user", session="s", at="a"),),
        )
    )
