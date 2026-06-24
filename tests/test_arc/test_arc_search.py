"""Thread D: semantic discovery over the ARC live context plane.

"Which expert/scope knows about X" — BM25 on the clio-core CTE backend (the
plain-text companion), naive word-overlap on LocalFS. CTE cases are integration.
"""

from __future__ import annotations

import pytest

from clio_agent.arc.memory import ARCMemory
from clio_agent.arc.storage import make_arc_store


def _seed(arc: ARCMemory, sid: str = "s1") -> str:
    arc.append_segment(sid, "agentA/hdf5", "observation",
                       {"text": "HDF5 dataset chunk sizes compression filters and dataset shapes"}, step=0)
    arc.append_segment(sid, "agentA/seismic", "observation",
                       {"text": "earthquake waveform catalog station picks magnitude and epicenter"}, step=0)
    arc.append_segment(sid, "agentA/wildfire", "observation",
                       {"text": "wildfire smoke plume dispersion air quality particulate forecast"}, step=0)
    return sid


def test_search_finds_the_right_scope_localfs(tmp_path):
    arc = ARCMemory(store=make_arc_store(backend="local", data_dir=str(tmp_path)))
    sid = _seed(arc)
    assert arc.segment_search_is_semantic() is False  # naive fallback
    hits = arc.search_segment_scopes(sid, "HDF5 chunking and compression", k=3)
    assert hits and hits[0][0] == "agentA/hdf5"


def test_search_scope_prefix_filter(tmp_path):
    arc = ARCMemory(store=make_arc_store(backend="local", data_dir=str(tmp_path)))
    sid = _seed(arc)
    arc.append_segment(sid, "agentB/other", "observation", {"text": "HDF5 chunk compression"}, step=0)
    hits = arc.search_segment_scopes(sid, "HDF5", scope_prefix="agentA/", k=5)
    assert hits and all(scope.startswith("agentA/") for scope, _ in hits)


def test_companion_is_not_a_record_and_drops_when_emptied(tmp_path):
    arc = ARCMemory(store=make_arc_store(backend="local", data_dir=str(tmp_path)))
    sid = _seed(arc)
    # the search companions never show up as scopes
    assert arc.list_segment_scopes(sid) == ["agentA/hdf5", "agentA/seismic", "agentA/wildfire"]
    # emptying a scope drops its companion -> it stops surfacing in search
    for seg in list(arc.render_segments(sid, "agentA/hdf5")):
        arc.delete_segments(sid, "agentA/hdf5", [seg.id])
    hits = arc.search_segment_scopes(sid, "HDF5 dataset compression filters", k=3)
    assert all(scope != "agentA/hdf5" for scope, _ in hits)


def test_search_empty_query(tmp_path):
    arc = ARCMemory(store=make_arc_store(backend="local", data_dir=str(tmp_path)))
    sid = _seed(arc)
    assert arc.search_segment_scopes(sid, "   ", k=3) == []


@pytest.mark.integration
def test_search_bm25_on_cte():
    arc = ARCMemory(store=make_arc_store(backend="cte"))
    sid = _seed(arc, sid="search_cte_s1")
    assert arc.segment_search_is_semantic() is True  # real BM25
    hits = arc.search_segment_scopes(sid, "earthquake magnitude and epicenter location", k=3)
    assert hits and hits[0][0] == "agentA/seismic"
    # different query -> different top scope (genuinely content-ranked)
    hits2 = arc.search_segment_scopes(sid, "smoke plume air quality", k=3)
    assert hits2 and hits2[0][0] == "agentA/wildfire"
    arc.clear_all()
