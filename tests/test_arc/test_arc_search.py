"""Thread D: semantic discovery over the ARC live context plane.

"Which expert/scope knows about X" — BM25 on clio-core (the plain-text companion),
served by the ``clio_cte_indexer`` chimod the seeded config composes (#905). A config
without it refuses search with a typed reason.
"""

from __future__ import annotations

import pytest

from clio_agent.arc.clio_core_indexer import CLIO_CORE_SEARCH_UNAVAILABLE
from clio_agent.arc.memory import ARCMemory, SearchUnavailableError
from clio_agent.arc.storage import make_arc_store


def _seed(arc: ARCMemory, sid: str = "s1") -> str:
    arc.append_segment(
        sid,
        "agentA/hdf5",
        "observation",
        {"text": "HDF5 dataset chunk sizes compression filters and dataset shapes"},
        step=0,
    )
    arc.append_segment(
        sid,
        "agentA/seismic",
        "observation",
        {"text": "earthquake waveform catalog station picks magnitude and epicenter"},
        step=0,
    )
    arc.append_segment(
        sid,
        "agentA/wildfire",
        "observation",
        {"text": "wildfire smoke plume dispersion air quality particulate forecast"},
        step=0,
    )
    return sid


def test_search_companions_are_never_listed_as_scopes():
    arc = ARCMemory(store=make_arc_store(backend="cte"))
    sid = _seed(arc)
    assert arc.list_segment_scopes(sid) == ["agentA/hdf5", "agentA/seismic", "agentA/wildfire"]


def test_search_ranks_the_scope_that_knows_about_the_query():
    arc = ARCMemory(store=make_arc_store(backend="cte"))
    sid = _seed(arc)
    assert arc.segment_search_is_semantic() is True
    hits = arc.search_segment_scopes(sid, "earthquake epicenter", k=3)
    assert [scope for scope, _score in hits][:1] == ["agentA/seismic"]


def test_search_is_a_typed_error_when_clio_core_cannot_search(monkeypatch):
    store = make_arc_store(backend="cte")
    monkeypatch.setattr(store, "_indexer_present", False)
    arc = ARCMemory(store=store)
    sid = _seed(arc)
    assert arc.segment_search_degradation_reason() == CLIO_CORE_SEARCH_UNAVAILABLE
    with pytest.raises(SearchUnavailableError) as caught:  # typed, never an empty ranking
        arc.search_segment_scopes(sid, "earthquake", k=3)
    assert caught.value.reason == CLIO_CORE_SEARCH_UNAVAILABLE
