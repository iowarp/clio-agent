"""Search reads stored records without making conversations resident (R6)."""

from __future__ import annotations

from types import SimpleNamespace

from clio_agent.arc.live import _MemoryStore
from clio_agent.arc.segments import SegmentStore
from clio_agent.gact.part_atoms import append_part_atom
from clio_agent.gact.transcript_reads import read_transcript, transcript_may_contain


def _app_with_atoms(texts: dict[str, list[str]]) -> SimpleNamespace:
    segments = SegmentStore(_MemoryStore())
    for sid, parts in texts.items():
        for index, text in enumerate(parts):
            append_part_atom(
                segments, sid, {"part": {"id": f"p{index}", "type": "text", "text": text}}
            )
    segments.clear()  # what a fresh process holds: nothing cached
    return SimpleNamespace(state=SimpleNamespace(arc=SimpleNamespace(_segments=segments)))


def test_a_session_whose_records_hold_no_term_is_ruled_out() -> None:
    app = _app_with_atoms({"s1": ["The Quarterly RESULTS are in"], "s2": ["nothing here"]})
    assert transcript_may_contain(app, "s1", ["results"])  # case-insensitive
    assert not transcript_may_contain(app, "s2", ["results"])
    assert not transcript_may_contain(app, "unknown", ["results"])
    assert app.state.arc._segments._loaded == set()  # raw reads: nothing was cached


def test_non_ascii_terms_match_their_lowercased_text() -> None:
    app = _app_with_atoms({"s": ["Über die Ergebnisse"]})
    assert transcript_may_contain(app, "s", ["über"])


def test_without_a_core_store_every_session_stays_a_candidate() -> None:
    app = SimpleNamespace(state=SimpleNamespace(arc=None))
    assert transcript_may_contain(app, "s", ["anything"])


def test_read_transcript_peeks_when_the_set_can() -> None:
    peeked: list[str] = []

    class _Set(dict):
        def peek(self, sid: str):  # type: ignore[no-untyped-def]
            peeked.append(sid)
            return ["row"]

    app = SimpleNamespace(state=SimpleNamespace(messages=_Set()))
    assert read_transcript(app, "s") == ["row"]
    assert peeked == ["s"]
    plain = SimpleNamespace(state=SimpleNamespace(messages={"s": ["a"]}))
    assert read_transcript(plain, "s") == ["a"]
    assert read_transcript(plain, "missing") == []
