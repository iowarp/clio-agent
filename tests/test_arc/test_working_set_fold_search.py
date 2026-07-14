"""Search-index equivalence: the fold's ingest-time ``.search`` companion (§2.7).

Under the fold, working-set content leaves the per-expert scope for the canonical
``_events/w`` content lane — which would orphan the per-scope ``.search`` companion
the scope-search surface (1.5) ranks over. The fold rewrites that companion at INGEST
time from the folded live render, so ``search_segment_scopes`` ranks a scope
identically whether the content was written the old way or folded. This test pins
that equivalence — the same content, searched under fold OFF and fold ON, must return
the same ``(scope, score)`` ranking — on BOTH backends, and after a mutation (so the
companion is proven to refresh, not go stale).
"""

from __future__ import annotations

import uuid

import pytest

from clio_agent.arc.memory import ARCMemory


@pytest.fixture(params=["local", "cte"])
def make_arc(request, tmp_path):
    """Factory for a fresh ARCMemory of a chosen fold-regime on the param backend."""
    backend = request.param
    created: list[ARCMemory] = []

    def _make(fold: bool) -> ARCMemory:
        if backend == "cte":
            pytest.importorskip("clio_cte_core_ext")
            from clio_agent.arc.storage import make_arc_store

            arc = ARCMemory(store=make_arc_store(backend="cte"), working_set_fold=fold)
        else:
            sub = tmp_path / ("on" if fold else "off")
            arc = ARCMemory(data_dir=str(sub), working_set_fold=fold)
        created.append(arc)
        return arc

    try:
        yield _make
    finally:
        for arc in created:
            try:
                arc.clear_all()
            except Exception:  # noqa: BLE001 - teardown best-effort on the shared runtime
                pass


def _populate(arc: ARCMemory, session: str) -> None:
    arc.append_segment(session, "agentA", "observation",
                       {"text": "alpha beta gamma the ocean tides rise"}, step=0)
    arc.append_segment(session, "agentA", "thought", {"text": "the tide is coming in"}, step=0)
    arc.append_segment(session, "agentB", "observation",
                       {"text": "quantum physics electrons spin state"}, step=0)


def test_search_ranking_identical_off_vs_on(make_arc) -> None:
    """Same content, searched under both regimes, yields the same scope ranking."""
    session = "srch_" + uuid.uuid4().hex[:12]
    off = make_arc(False)
    on = make_arc(True)
    _populate(off, session)
    _populate(on, session)
    for query in ("ocean tides", "quantum electrons", "tide coming"):
        r_off = off.search_segment_scopes(session, query, k=5)
        r_on = on.search_segment_scopes(session, query, k=5)
        assert r_off == r_on, f"search ranking diverged for {query!r}: off={r_off} on={r_on}"


def test_search_companion_refreshes_after_delete(make_arc) -> None:
    """After a delete removes the only match, the fold's companion refreshes so the
    scope no longer ranks for the deleted text — identically to the old write."""
    session = "srch_" + uuid.uuid4().hex[:12]
    off = make_arc(False)
    on = make_arc(True)
    for arc in (off, on):
        arc.append_segment(session, "agentA", "observation",
                           {"text": "unicorn rainbow sparkle"}, step=0)
        arc.append_segment(session, "agentA", "observation",
                           {"text": "ordinary grey pavement"}, step=0)
        target = [s for s in arc.render_segments(session, "agentA")
                  if "unicorn" in s.content.get("text", "")][0]
        arc.delete_segments(session, "agentA", [target.id])
    r_off = off.search_segment_scopes(session, "unicorn rainbow", k=5)
    r_on = on.search_segment_scopes(session, "unicorn rainbow", k=5)
    assert r_off == r_on, f"post-delete search diverged: off={r_off} on={r_on}"
