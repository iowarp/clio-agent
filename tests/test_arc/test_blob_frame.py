"""A record that gets shorter reads back exactly, on real clio-core.

clio-core's ``PutBlob`` at offset 0 over a longer blob can keep the old tail (measured on
iowarp-core 2.2.1: 976 -> 940 base64 characters reads back 976). Found live: after a
compaction moved an anchor, the session index got 36 characters shorter and a restarted
server failed every turn with ``undecodable_index`` (trailing characters).

Sabotage: ``ClioCoreStore.put`` storing plain ``base64.b64encode(data)`` (no frame)
turns ``test_a_slightly_shorter_put_reads_back_exactly`` and
``test_a_shrunk_session_index_decodes_after_a_restart`` red.
"""

from __future__ import annotations

import base64
import uuid

import pytest

from clio_agent.arc.batch_put import PutRecord
from clio_agent.arc.blob_frame import BlobDecodeError, BlobFrameError, frame, unframe
from clio_agent.arc.lane_index import (
    INDEX_SCOPE,
    ChunkEntry,
    ScopeEntry,
    SessionIndex,
    decode_index,
    encode_index,
)
from clio_agent.arc.storage import ClioCoreStore, make_arc_store


def _store(namespace: str) -> ClioCoreStore:
    store = make_arc_store(backend="cte", namespace=namespace)
    assert isinstance(store, ClioCoreStore)
    return store


def test_frame_round_trips_and_ignores_a_stale_tail() -> None:
    data = b"\x00\x01payload:with:colons"
    body = frame(data)
    assert unframe("r", body) == data
    assert unframe("r", body + b"QUJDREVGR0g=") == data  # an older, longer body's tail
    assert unframe("r", body.decode("ascii")) == data  # the binding may hand back str


def test_a_body_stored_before_the_frame_reads_whole() -> None:
    assert unframe("r", base64.b64encode(b"legacy record")) == b"legacy record"


def test_a_truncated_frame_is_typed() -> None:
    with pytest.raises(BlobFrameError) as caught:
        unframe("r", frame(b"x" * 30)[:-4])
    assert caught.value.error_type == "clio_core_blob_truncated"


def test_invalid_base64_is_typed_for_legacy_and_framed_blobs() -> None:
    for body in (b"invalid!", b"8:invalid!"):
        with pytest.raises(BlobDecodeError) as caught:
            unframe("damaged", body)
        assert caught.value.error_type == "clio_core_blob_invalid_base64"


def test_a_slightly_shorter_put_reads_back_exactly() -> None:
    store = _store(f"frame-{uuid.uuid4().hex[:10]}")
    store.put("segments", "r", b"A" * 732)
    store.put("segments", "r", b"B" * 705)
    assert store.get("segments", "r") == b"B" * 705
    store.put_many("segments", [PutRecord(name="r", data=b"C" * 690)])
    assert store.get("segments", "r") == b"C" * 690


def _index_of_size(size: int) -> SessionIndex:
    """An index whose record is exactly ``size`` bytes (the live sizes: 732 -> 705)."""
    entry = ChunkEntry(partition="_events/w/sp", index=1, min_lt=1, scopes=["a"])
    filler = ""
    while True:
        index = SessionIndex(
            chunks={"_events/w/sp": entry}, scopes={"a": ScopeEntry(chunks=[filler])}
        )
        if len(encode_index(index)) >= size:
            assert len(encode_index(index)) == size
            return index
        filler += "x"


def test_a_shrunk_session_index_decodes_after_a_restart() -> None:
    namespace = f"frame-{uuid.uuid4().hex[:10]}"
    name = f"s1__{INDEX_SCOPE}"
    grown, shrunk = _index_of_size(732), _index_of_size(705)
    writer = _store(namespace)
    writer.put("segments", name, encode_index(grown))
    writer.put("segments", name, encode_index(shrunk))
    reader = _store(namespace)  # a restarted process: no client-side state
    raw = reader.get("segments", name)
    assert raw is not None
    assert decode_index("s1", raw) == shrunk
