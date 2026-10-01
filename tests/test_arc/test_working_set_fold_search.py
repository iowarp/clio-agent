"""The working set's search companion (§2.7, Phase 11a), on clio-core.

Content lives on the search-excluded ``_events/w`` lane, so the fold writes a search
companion at ingest: append-only text chunks per logical scope (``_search/<scope>/<lt>``)
covering EVERY atom ever written -- a deleted or compacted atom stays findable, and a
hit says which of its atoms are no longer live (marked at query time from the view).

clio-core's BM25 itself is unavailable while the iowarp-core wheels ship no indexer
chimod (#905), so these tests read the companions clio-core holds directly and resolve
a chunk the way a search hit is resolved (``FoldingSegmentStore.resolve_hit``); the
search entry point is pinned to fail typed.

Sabotage: making ``SearchCompanion.add`` skip retired-to-be atoms (or rewriting the
chunk from the live view) turns ``test_a_compacted_atom_stays_findable_and_is_marked``
red; dropping the ``is_retired`` marking turns the compacted assertion red.
"""

from __future__ import annotations

import uuid

import pytest

from clio_agent.arc.memory import ARCMemory, SearchUnavailableError
from clio_agent.arc.search_companion import parse_search_scope
from clio_agent.arc.storage import make_arc_store
from clio_agent.arc.working_set_fold import FoldingSegmentStore


@pytest.fixture
def arc():
    memory = ARCMemory(
        store=make_arc_store(backend="cte", namespace=f"srch-{uuid.uuid4().hex[:8]}")
    )
    yield memory
    memory._store.clear()


def _companions(arc: ARCMemory, session: str) -> dict[str, str]:
    """``{companion record scope: its text}`` as clio-core holds them."""
    store = arc._store
    head = f"{session}__"
    tag = store._cte.Tag(store.tag("segments"))
    out: dict[str, str] = {}
    for name in tag.GetContainedBlobs():
        if not name.startswith(f"{head}_search~") or not name.endswith(".text"):
            continue
        size = tag.GetBlobSize(name)
        raw = tag.GetBlob(name, size, 0)
        stem = name[len(head) : -len(".text")]
        out[FoldingSegmentStore.scope_of_record(stem)] = (
            raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)
        )
    return out


def _find(arc: ARCMemory, session: str, word: str) -> list:
    """Resolve every companion chunk whose text holds ``word`` (BM25's stand-in)."""
    folding = arc._segments
    assert isinstance(folding, FoldingSegmentStore)
    return [
        folding.resolve_hit(session, record_scope, 1.0)
        for record_scope, text in _companions(arc, session).items()
        if word in text
    ]


def test_every_atom_is_in_its_scopes_companion(arc: ARCMemory) -> None:
    session = "srch_" + uuid.uuid4().hex[:12]
    arc.append_segment(session, "agentA", "observation", {"text": "the ocean tides rise"}, step=0)
    arc.append_segment(session, "agentA", "thought", {"text": "the tide is coming in"}, step=0)
    arc.append_segment(session, "agentB", "observation", {"text": "electrons spin state"}, step=0)

    companions = _companions(arc, session)
    by_scope = {parse_search_scope(k)[0]: v for k, v in companions.items()}  # type: ignore[index]
    assert by_scope == {
        "agentA": "the ocean tides rise\nthe tide is coming in",
        "agentB": "electrons spin state",
    }
    assert arc.list_segment_scopes(session) == ["agentA", "agentB"]  # never the chunks


def test_a_compacted_atom_stays_findable_and_is_marked(arc: ARCMemory) -> None:
    session = "srch_" + uuid.uuid4().hex[:12]
    gone = arc.append_segment(session, "agentA", "observation", {"text": "unicorn rainbow"})
    kept = arc.append_segment(session, "agentA", "observation", {"text": "grey pavement"})
    summary = arc.summarize_segments(session, "agentA", [gone.id, kept.id], {"text": "roads"})
    later = arc.append_segment(session, "agentA", "observation", {"text": "unicorn again"})

    [hit] = _find(arc, session, "unicorn rainbow")
    marks = {a.atom_id: a.compacted for a in hit.atoms}
    assert marks[gone.id] is True and marks[kept.id] is True
    assert marks[summary.id] is False and marks[later.id] is False
    assert hit.scope == "agentA"

    # A restarted process (cold view from the anchor) marks it the same way.
    cold = ARCMemory(store=arc._store)
    [cold_hit] = _find(cold, session, "unicorn rainbow")
    assert {a.atom_id: a.compacted for a in cold_hit.atoms} == marks


def test_a_deleted_atom_stays_findable_and_is_marked(arc: ARCMemory) -> None:
    session = "srch_" + uuid.uuid4().hex[:12]
    gone = arc.append_segment(session, "agentA", "observation", {"text": "unicorn rainbow"})
    arc.append_segment(session, "agentA", "observation", {"text": "grey pavement"})
    arc.delete_segments(session, "agentA", [gone.id])

    [hit] = _find(arc, session, "unicorn")
    assert {a.atom_id for a in hit.atoms if a.compacted} == {gone.id}


def test_search_itself_is_typed_unavailable(arc: ARCMemory) -> None:
    with pytest.raises(SearchUnavailableError):
        arc.search_segment_scopes("s", "ocean", k=5)
    with pytest.raises(SearchUnavailableError):
        arc.search_context("s", "ocean", k=5)
