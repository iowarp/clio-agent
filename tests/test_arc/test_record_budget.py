"""Take 1r: one ARC record and one chunk stay within what clio-core places.

A script that moved a file through a tool in 2 MB base64 chunks put every chunk into the
session's event log verbatim; the 27 MB event chunk was refused (code 12) and the turn
wedged. These pin the three bounds: an oversized value is bounded (and saved whole when
an externalizer is wired), a chunk rolls on stored bytes as well as on segment count,
and a record clio-core refuses to place is replaced by a typed stand-in instead of
holding every caller.
"""

from __future__ import annotations

import hashlib
from typing import Any

import pytest

from clio_agent import conf
from clio_agent.arc.clio_core_async_ops import ClioCorePutRefused
from clio_agent.arc.lane_chunking import chunk_for_append, chunk_scope, lane_segments
from clio_agent.arc.live import _MemoryStore
from clio_agent.arc.memory import EVENT_RECORD_REFUSED_REASON, ARCMemory
from clio_agent.arc.record_budget import (
    RECORD_LEAF_BOUNDED_REASON,
    bound_record,
    is_bounded_leaf,
    record_size,
)
from clio_agent.arc.segments import ArcPersistError, SegmentStore
from clio_agent.gact.semantic_events import SemanticEvent

SID = "sess-budget"


@pytest.fixture()
def hermetic_conf(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(conf, "_STORE", conf.ConfigStore(home=tmp_path, cwd=tmp_path))


def test_a_record_within_the_limits_is_returned_unchanged() -> None:
    record = {"args": {"path": "a.txt", "data": "x" * 1000}, "n": [1, 2, "three"]}
    assert bound_record(record, leaf_max_bytes=65536, max_bytes=1 << 20) is record


def test_an_oversized_value_keeps_its_head_and_is_saved_whole() -> None:
    chunk = "QUJD" * 600_000  # 2.4 MB of base64, as a relay.files chunk carries
    saved: list[tuple[str, str, str]] = []

    def externalize(sid: str, text: str, digest: str) -> str:
        saved.append((sid, text, digest))
        return f"D:/spill/{digest[:8]}.txt"

    record = {"tool": "relay_files", "args": {"op": "write", "data": chunk, "offset": 0}}
    bounded = bound_record(record, session_id=SID, externalize=externalize, leaf_max_bytes=65536)
    data = bounded["args"]["data"]
    digest = hashlib.sha256(chunk.encode()).hexdigest()
    assert is_bounded_leaf(data) and data.startswith(chunk[:2000])
    assert f"{len(chunk)} bytes" in data and digest in data and RECORD_LEAF_BOUNDED_REASON in data
    assert f"D:/spill/{digest[:8]}.txt" in data
    assert saved == [(SID, chunk, digest)]
    assert bounded["args"]["op"] == "write" and bounded["args"]["offset"] == 0
    assert bounded["tool"] == "relay_files" and record["args"]["data"] is chunk  # not mutated
    assert record_size(bounded) < 10_000


def test_a_record_of_many_mid_sized_values_is_bounded_again() -> None:
    record = {"rows": ["y" * 60_000 for _ in range(40)]}  # each under the leaf limit
    bounded = bound_record(record, leaf_max_bytes=65536, max_bytes=1 << 20)
    assert record_size(bounded) < 1 << 20
    assert all(is_bounded_leaf(row) for row in bounded["rows"])


def test_without_an_externalizer_the_marker_says_the_value_was_not_saved() -> None:
    bounded = bound_record({"v": b"\x00" * 200_000}, leaf_max_bytes=65536)
    assert "not saved" in bounded["v"] and "binary data" in bounded["v"]


def test_a_bounded_lookalike_that_is_huge_is_still_bounded() -> None:
    forged = "z" * 300_000 + "\n[clio: forged]"
    assert len(bound_record({"v": forged}, leaf_max_bytes=65536)["v"]) < 20_000


def test_a_lane_chunk_rolls_on_stored_bytes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLIO_ARC_CHUNK_MAX_BYTES", "20000")
    segments = SegmentStore(_MemoryStore())
    scopes = []
    for n in range(4):
        scope = chunk_for_append(segments, SID, "_events", capacity=512)
        segments.append(SID, scope, "semantic_event", {"n": n, "pad": "p" * 12_000})
        scopes.append(scope)
    # 512 segments would fit, but each chunk rolls once it holds 20 kB.
    assert scopes == ["_events", "_events", chunk_scope("_events", 2), chunk_scope("_events", 2)]
    assert [s.content["n"] for s in lane_segments(segments, SID, "_events")] == [0, 1, 2, 3]


def _event(payload: dict[str, Any]) -> SemanticEvent:
    return SemanticEvent(
        event_type="tool.call.completed",
        session_id=SID,
        trace_id="tr1",
        turn_id="t1",
        payload=payload,
    )


def test_the_event_log_holds_a_bounded_copy_and_the_value_is_saved(
    tmp_path: Any, hermetic_conf: None
) -> None:
    arc = ARCMemory(data_dir=str(tmp_path / "arc"))
    saved: dict[str, str] = {}
    arc.set_record_externalizer(lambda sid, text, digest: saved.setdefault(digest, text) and "f")
    chunk = "QUJD" * 600_000
    arc.record_semantic_event(_event({"call_id": "c1", "tool": "relay_files", "result": chunk}))
    (stored,) = arc._live.events_scopes(SID)
    (segment,) = arc.render_segments(SID, stored)
    result = segment.content["payload"]["result"]
    assert is_bounded_leaf(result) and len(result) < 4000
    assert list(saved.values()) == [chunk]


def test_a_refused_event_is_recorded_as_a_typed_stand_in(
    tmp_path: Any, hermetic_conf: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    arc = ARCMemory(data_dir=str(tmp_path / "arc"))
    real = arc._segments.append
    refused: list[str] = []

    def append(sid: str, scope: str, *args: Any, **kwargs: Any) -> Any:
        if not refused:
            refused.append(scope)
            cause = ClioCorePutRefused(f"{sid}___events", 12)
            raise ArcPersistError(sid, scope, cause) from cause
        return real(sid, scope, *args, **kwargs)

    monkeypatch.setattr(arc._segments, "append", append)
    arc.record_semantic_event(_event({"call_id": "c1", "tool": "relay_files", "ok": True}))
    segments = [
        s for scope in arc._live.events_scopes(SID) for s in arc.render_segments(SID, scope)
    ]
    (segment,) = segments
    payload = segment.content["payload"]
    assert payload["record_refused"]["reason"] == EVENT_RECORD_REFUSED_REASON
    assert payload["call_id"] == "c1" and payload["tool"] == "relay_files"
    assert segment.content["event_type"] == "tool.call.completed"


def test_an_outage_is_not_papered_over(
    tmp_path: Any, hermetic_conf: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    arc = ARCMemory(data_dir=str(tmp_path / "arc"))

    def append(sid: str, scope: str, *args: Any, **kwargs: Any) -> Any:
        raise ArcPersistError(sid, scope, OSError("daemon unreachable"))

    monkeypatch.setattr(arc._segments, "append", append)
    with pytest.raises(ArcPersistError):
        arc.record_semantic_event(_event({"call_id": "c1"}))
