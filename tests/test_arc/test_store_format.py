"""Which CLIO wrote a clio-core store, read from the store itself (arc/store_format.py).

A store written by CLIO 0.9.5.1 or older holds its segment lists as framed base64
(``b"<n>:" + base64``); this CLIO writes records that start with the chunk magic. The
check lists a bounded number of segment-list names, reads the smallest few, and says
``current``, ``empty``, ``old`` or ``unknown`` -- never a guess from a version number.
"""

from __future__ import annotations

import base64

import pytest

from clio_agent.arc.chunk_records import MAGIC, read_chunk
from clio_agent.arc.lane_index import INDEX_SCOPE, SessionIndex, encode_index
from clio_agent.arc.memory import ARCMemory
from clio_agent.arc.schema import encode_segments
from clio_agent.arc.search_companion import SEARCH_FAMILY
from clio_agent.arc.store_format import (
    ARC_STORE_FORMAT_OLD,
    ARC_STORE_FORMAT_UNKNOWN,
    classify_head,
    inspect_store,
)
from clio_agent.gact.semantic_events import SemanticEvent


def _conversation(arc: ARCMemory, session_id: str) -> str:
    """One conversation's event log, as this CLIO stores it; its record name."""
    arc.record_semantic_event(
        SemanticEvent(
            event_type="lm.call",
            session_id=session_id,
            trace_id="t",
            span_id="s",
            payload={"prompt": "hello"},
        )
    )
    return arc._segments._record_name(session_id, "_events")


def _as_v0951_stored_it(arc: ARCMemory, name: str) -> None:
    """Rewrite one record as CLIO 0.9.5.1 stored it: one framed base64 put."""
    raw = arc._segments._store
    parsed = read_chunk(raw, "segments", name)
    assert parsed is not None and parsed.segments
    raw.put("segments", name, encode_segments(parsed.segments))


@pytest.mark.parametrize(
    ("head", "kind"),
    [
        (MAGIC + b"\x10\x00\x00\x00", "current"),
        (b"1288:" + base64.b64encode(b"\x91\x85\xa2id"), "old"),
        (base64.b64encode(encode_segments([]) * 12)[:16], "old"),  # before the frame
        (b"\x93\x01\x02\x03", "unrecognized"),
        (b"", "unrecognized"),
    ],
)
def test_a_record_head_names_its_format(head: bytes, kind: str) -> None:
    assert classify_head(head) == kind


def test_a_store_this_clio_wrote_is_current(arc: ARCMemory) -> None:
    _conversation(arc, "sess_new")
    verdict = inspect_store(arc._segments._store)
    assert verdict.verdict == "current"
    assert verdict.counts["current"] >= 1 and verdict.reason == ""


def test_a_store_with_no_conversation_is_empty(arc: ARCMemory) -> None:
    verdict = inspect_store(arc._segments._store)
    assert verdict.verdict == "empty"


def test_a_store_written_by_v0951_is_old(arc: ARCMemory) -> None:
    """SABOTAGE: classify the retired frame as current in ``classify_head`` and this
    store reads as ``current``: the installer would leave it and the boot would open it."""
    _as_v0951_stored_it(arc, _conversation(arc, "sess_old"))
    verdict = inspect_store(arc._segments._store)
    assert verdict.verdict == "old"
    assert verdict.reason == ARC_STORE_FORMAT_OLD
    assert verdict.as_dict()["records_read"]["old"] == 1


def test_one_old_record_among_new_ones_is_still_old(arc: ARCMemory) -> None:
    """A newer CLIO run on an old store (a manual upgrade) leaves both formats."""
    _conversation(arc, "sess_new")
    _as_v0951_stored_it(arc, _conversation(arc, "sess_old"))
    assert inspect_store(arc._segments._store).verdict == "old"


def _framed_in_both_formats(arc: ARCMemory, session_id: str) -> None:
    """The lane index and a search-companion chunk: still one framed put each."""
    store = arc._segments._store
    name = arc._segments._record_name
    store.put("segments", name(session_id, INDEX_SCOPE), encode_index(SessionIndex()))
    store.put("segments", name(session_id, f"{SEARCH_FAMILY}/_events/1"), b"\x90")


def test_records_framed_in_both_formats_never_make_a_new_store_old(arc: ARCMemory) -> None:
    """SABOTAGE: sample every listed name in ``_segment_names`` and the small framed
    index reads as the retired frame: a store this CLIO just wrote is refused at boot."""
    _conversation(arc, "sess_new")
    _framed_in_both_formats(arc, "sess_new")
    verdict = inspect_store(arc._segments._store)
    assert verdict.verdict == "current"
    assert verdict.counts["old"] == 0


def test_only_records_framed_in_both_formats_is_unknown_not_empty(arc: ARCMemory) -> None:
    _framed_in_both_formats(arc, "sess_x")
    verdict = inspect_store(arc._segments._store)
    assert verdict.verdict == "unknown"
    assert verdict.error == "no format-telling record"


def test_an_unrecognized_record_is_unknown_never_a_guess(arc: ARCMemory) -> None:
    arc._segments._store.put_at("segments", "sess_x~_events", b"\x93garbage-bytes!", 0)
    verdict = inspect_store(arc._segments._store)
    assert verdict.verdict == "unknown"
    assert verdict.reason == ARC_STORE_FORMAT_UNKNOWN


def test_an_unreadable_store_is_unknown_with_its_error() -> None:
    class Refusing:
        def query_names(self, kind: str, name_re: str = "", *, limit: int = 0) -> list[str]:
            raise RuntimeError("BlobQuery failed")

    verdict = inspect_store(Refusing())
    assert verdict.verdict == "unknown"
    assert "BlobQuery failed" in verdict.error


def test_the_check_asks_clio_core_for_a_bounded_number_of_names(arc: ARCMemory) -> None:
    """The boot check is cheap: one name query bounded by ``limit``, whatever the size."""
    asked: list[int] = []
    store = arc._segments._store
    real = store.query_names

    def bounded(kind: str, name_re: str = "", *, limit: int = 0) -> list[str]:
        asked.append(limit)
        return real(kind, name_re, limit=limit)

    for index in range(3):
        _conversation(arc, f"sess_{index}")
    store.query_names = bounded  # type: ignore[method-assign]
    try:
        assert inspect_store(store).verdict == "current"
    finally:
        del store.query_names
    assert asked and all(0 < limit <= 32 for limit in asked)
    assert len(real("segments", ".*", limit=1)) == 1
