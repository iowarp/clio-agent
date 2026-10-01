"""Search-companion equivalence under the working-set fold (§2.7), on clio-core.

Under the fold, working-set content leaves the per-expert scope for the canonical
``_events/w`` content lane, which would orphan the per-scope search companion (the
plain-text projection a search index ranks). The fold rewrites that companion at
ingest from the folded live render. This pins that the companion clio-core holds is
the SAME text whether the content was written the old way (fold off) or folded, and
that it refreshes after a delete.

It reads the companions directly: clio-core's search itself is unavailable while the
iowarp-core wheels ship no indexer chimod (#905), so ranking cannot be compared.
"""

from __future__ import annotations

import uuid

import pytest

from clio_agent.arc.memory import ARCMemory, SearchUnavailableError
from clio_agent.arc.storage import make_arc_store


def _arc(fold: bool, namespace: str) -> ARCMemory:
    return ARCMemory(
        store=make_arc_store(backend="cte", namespace=namespace), working_set_fold=fold
    )


def _companion(arc: ARCMemory, session: str, scope: str) -> str:
    """The raw companion blob (written as plain text, so not through ``get``'s decode)."""
    store = arc._store
    name = arc._segments._record_name(session, scope) + ".text"
    tag = store._cte.Tag(store.tag("segments"))
    size = tag.GetBlobSize(name)
    if size == 0:
        return ""
    raw = tag.GetBlob(name, size, 0)
    return raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)


def _populate(arc: ARCMemory, session: str) -> None:
    arc.append_segment(session, "agentA", "observation", {"text": "the ocean tides rise"}, step=0)
    arc.append_segment(session, "agentA", "thought", {"text": "the tide is coming in"}, step=0)
    arc.append_segment(session, "agentB", "observation", {"text": "electrons spin state"}, step=0)


@pytest.fixture
def pair():
    tag = uuid.uuid4().hex[:8]
    off, on = _arc(False, f"fold-off-{tag}"), _arc(True, f"fold-on-{tag}")
    yield off, on
    off._store.clear()
    on._store.clear()


def test_the_companion_is_identical_with_the_fold_off_or_on(pair) -> None:
    off, on = pair
    session = "srch_" + uuid.uuid4().hex[:12]
    _populate(off, session)
    _populate(on, session)
    for scope in ("agentA", "agentB"):
        assert _companion(off, session, scope)
        assert _companion(off, session, scope) == _companion(on, session, scope)


def test_the_companion_refreshes_after_a_delete(pair) -> None:
    off, on = pair
    session = "srch_" + uuid.uuid4().hex[:12]
    for arc in (off, on):
        arc.append_segment(session, "agentA", "observation", {"text": "unicorn rainbow"}, step=0)
        arc.append_segment(session, "agentA", "observation", {"text": "grey pavement"}, step=0)
        target = [
            s for s in arc.render_segments(session, "agentA") if "unicorn" in s.content["text"]
        ][0]
        arc.delete_segments(session, "agentA", [target.id])
    for arc in (off, on):
        assert "unicorn" not in _companion(arc, session, "agentA")
    assert _companion(off, session, "agentA") == _companion(on, session, "agentA")


def test_search_itself_is_typed_unavailable(pair) -> None:
    off, _on = pair
    with pytest.raises(SearchUnavailableError):
        off.search_segment_scopes("s", "ocean", k=5)
