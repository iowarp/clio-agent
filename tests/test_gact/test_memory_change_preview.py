"""The semantic preview follows retained fact lineage and never guesses corrections."""

from clio_agent.arc import memory_document as md
from clio_agent.gact.memory.change_preview import fact_changes, inline_change


def test_correction_pairs_only_recorded_supersession_and_ignores_existing_history() -> None:
    old = md.Fact(
        id="faaa", text="Favorite color is red.", source="user", session="s", at="2026-10-25"
    )
    new = md.Fact(
        id="fbbb", text="Favorite color is blue.", source="user", session="s", at="2026-10-25"
    )
    before = md.MemoryDocument(
        name="colors",
        description="Color preferences",
        category="topics",
        updated="2026-10-25",
        facts=(old,),
    )
    after = md.superseding(before, old.id, new, updated="2026-10-25")
    assert fact_changes(md.serialize(before), md.serialize(after)) == [
        {"id": "fbbb", "kind": "updated", "before": old.text, "after": new.text}
    ]
    assert fact_changes(md.serialize(after), md.serialize(after)) == []
    assert fact_changes(md.serialize(before), "") == [
        {"id": "faaa", "kind": "removed", "before": old.text, "after": ""}
    ]
    assert fact_changes("", md.serialize(before)) == [
        {"id": "faaa", "kind": "added", "before": "", "after": old.text}
    ]
    assert fact_changes("truncated snapshot", md.serialize(after)) is None


def test_inline_correction_keeps_common_words_and_escapes_fact_markdown() -> None:
    assert (
        inline_change("Favorite color is red.", "Favorite color is blue.")
        == r"Favorite color is ~~red~~ **blue**\."
    )
    assert "[" not in inline_change("red", "[blue](https://example.org)").replace("\\[", "")
