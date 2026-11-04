"""Recall exposes supported modes rather than inviting known-unavailable searches."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from clio_agent.gact import context
from clio_agent.gact import recall_context_tool as recall
from clio_agent.gact.summarization_record import recall_line


@pytest.mark.parametrize("search_enabled", [False, True])
def test_recall_schema_and_summary_match_the_actual_context_capability(
    monkeypatch: pytest.MonkeyPatch, search_enabled: bool
) -> None:
    """ID recall remains available and byte-exact with or without an indexer."""
    segment = SimpleNamespace(
        id="step1",
        scope="main",
        kind="observation",
        status="tombstoned",
        content={"text": "retained original evidence"},
        derived_from=[],
    )
    arc = SimpleNamespace(
        segment_search_is_semantic=lambda: search_enabled,
        list_segment_scopes=lambda _sid: ["main"],
        list_segments=lambda _sid, _scope, **_kwargs: [segment],
        search_context=lambda *_args, **_kwargs: [
            SimpleNamespace(atoms=[SimpleNamespace(atom_id="step1")])
        ],
    )
    app = SimpleNamespace(state=SimpleNamespace(arc=arc))
    monkeypatch.setattr(context, "active_app", lambda: app)
    monkeypatch.setattr(recall, "_plane", lambda: (arc, "session"))
    tool: Any = recall.build_recall_context_tool()
    assert set(tool.args) == ({"ids", "query", "limit"} if search_enabled else {"ids"})
    assert tool(ids=["step1"]).endswith(
        "--- step1 · result · compacted\nretained original evidence"
    )
    footer = recall_line("cmp_example", search_enabled=search_enabled)
    assert 'recall_context(ids=["cmp_example"])' in footer
    if search_enabled:
        assert "--- step1 · result" in tool(query="evidence")
        assert 'recall_context(query="...")' in footer
    else:
        assert "unavailable" in tool.desc
        assert "recall_context(query=" not in footer


def _seg(atom_id: str, kind: str, content: dict[str, Any], lt: int) -> SimpleNamespace:
    return SimpleNamespace(
        id=atom_id, scope="main", kind=kind, status="tombstoned", content=content,
        derived_from=[], logical_time=lt,
    )  # fmt: skip


def _arc(segments: list[SimpleNamespace], chunks: list[list[str]]) -> SimpleNamespace:
    """A store whose search returns ``chunks`` (atom ids each), best first."""
    return SimpleNamespace(
        list_segment_scopes=lambda _sid: ["main"],
        list_segments=lambda _sid, _scope, **_kwargs: segments,
        search_context=lambda *_args, **_kwargs: [
            SimpleNamespace(atoms=[SimpleNamespace(atom_id=i) for i in chunk]) for chunk in chunks
        ],
    )


def test_a_query_returns_the_steps_with_its_words_oldest_first() -> None:
    """A hit is a chunk of many atoms: only the ones with the query's words come back,
    the most words first within ``limit``, then shown oldest first; delivery records
    (no text) and reasoning-only thoughts are left out and counted.

    SABOTAGE: return every atom of the hit chunks and the bookkeeping record, the
    reasoning and the unrelated call all come back, past the limit of 2."""
    segments = [
        _seg("first", "thought", {"text": "The two snapshots are identical for fc1."}, 1),
        _seg("call", "tool_call", {"name": "shell_bash", "args": {"command": "ls"}}, 2),
        _seg("deliv", "recipient_input", {"schema_version": 1, "content": []}, 3),
        _seg("why", "thought", {"text": "", "thinking": [{"text": "snapshot musing"}]}, 4),
        _seg("later", "thought", {"text": "Leaving out the second snapshot changes nothing."}, 5),
        _seg("ask", "user", {"text": "What did you say about the second snapshot?"}, 6),
    ]
    arc = _arc(segments, [["ask", "deliv", "why", "later"], ["first", "call"]])
    rows, left_out, rest = recall.recall_atoms(arc, "s", query="second snapshot", limit=2)
    assert [r["id"] for r in rows] == ["later", "ask"]
    assert rest == ["first"] and left_out == 2
    text = recall.recall_text(rows, left_out=left_out, rest=rest)
    assert text.startswith("2 of 3 steps, oldest first. 2 records without text left out")
    assert 'recall_context(ids=["first"])' in text
    assert text.index("--- later · said · compacted") < text.index("--- ask · message")
    assert "musing" not in text and "schema_version" not in text


def test_a_compaction_id_past_the_cap_names_the_rest() -> None:
    """SABOTAGE: drop ``rest`` and the steps past 50 vanish without a word."""
    segments = [_seg(f"s{i:02d}", "observation", {"text": f"out {i}"}, i) for i in range(53)]
    summary = SimpleNamespace(
        id="sum", scope="main", kind="summary", status="live", logical_time=99,
        content={"text": "summary", "compaction_id": "cmp_1"},
        derived_from=[s.id for s in segments],
    )  # fmt: skip
    rows, left_out, rest = recall.recall_atoms(_arc([*segments, summary], []), "s", ids=["cmp_1"])
    assert len(rows) == 50 and rest == ["s50", "s51", "s52"] and left_out == 0
    assert "50 of 53 steps" in recall.recall_text(rows, left_out=0, rest=rest)
