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
import inspect
import os
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from clio_agent.arc.batch_put import PutRecord
from clio_agent.arc.blob_frame import (
    BlobDecodeError,
    BlobFrameError,
    frame,
    size_class,
    stored_body,
    unframe,
)
from clio_agent.arc.clio_core_config import effective_ram_cap
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


@pytest.mark.parametrize("as_text", [False, True])
@pytest.mark.parametrize("physically_truncated", [False, True])
def test_get_distinguishes_stale_size_from_truncated_data(
    as_text: bool, physically_truncated: bool
) -> None:
    """Size metadata can lag intact bytes; genuinely missing bytes remain an error."""
    data = b"retained assignment observations" * 40
    stored = frame(data)
    stale_size = len(stored) - 16
    reads: list[int] = []

    def read_blob(name: str, size: int, offset: int) -> bytes | str:
        assert name == "record" and offset == 0
        reads.append(size)
        available = stored[:stale_size] if physically_truncated else stored
        result = available[:size]
        return result.decode("ascii") if as_text else result

    tag = SimpleNamespace(GetBlobSize=lambda name: stale_size, GetBlob=read_blob)
    store = SimpleNamespace(_cte=SimpleNamespace(Tag=lambda name: tag), tag=lambda kind: kind)
    # Exercise the read algorithm independently of every separately covered RPC wrapper.
    read = cast(Any, inspect.unwrap(ClioCoreStore._get_one))
    if physically_truncated:
        with pytest.raises(BlobFrameError):
            read(store, "segments", "record")
    else:
        assert read(store, "segments", "record") == data
    assert reads == [stale_size, len(stored)]


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


_PAGE = 4096


def test_size_classes_are_pages_at_most_a_quarter_above_the_page_rounded_size() -> None:
    previous = 0
    for length in [*range(1, 70_000, 37), *range(70_000, 6_000_000, 9_973)]:
        size = size_class(length)
        page_rounded = -(-length // _PAGE) * _PAGE
        assert size % _PAGE == 0 and size >= length
        assert size <= page_rounded * 1.25
        assert size >= previous  # monotone: a record that grows never drops a class
        previous = size
    # few classes: a 1 MB record crosses about 25 of them, not one per append
    assert len({size_class(n) for n in range(1, 1_000_001, 101)}) <= 26


def test_a_stored_body_is_its_frame_padded_to_its_class() -> None:
    data = b"\x00\x01payload:with:colons" * 50
    body = stored_body(data)
    assert len(body) == size_class(len(frame(data)))
    assert body.startswith(frame(data))
    assert set(body[len(frame(data)) :]) <= {ord(".")}
    assert unframe("r", body) == data
    assert unframe("r", body.decode("ascii")) == data


def _metadata_log_bytes() -> int:
    """Bytes of the attached daemon's ``metadata.log*`` files (the test daemon's own)."""
    config = effective_ram_cap(env=os.environ).config_path
    assert config is not None
    root = Path(config).parent
    return sum(path.stat().st_size for path in root.rglob("metadata.log*"))


def test_a_regrown_record_writes_little_metadata_log() -> None:
    """A lane chunk is re-put after every append. Each put that grows a clio-core blob
    adds a block and each put logs the blob's whole block list, so unpadded bodies wrote
    ~3.6 MB of metadata log for these 300 appends (80 bytes per block per put); size
    classes keep it to a few hundred KB.

    Sabotage: ``ClioCoreStore.put`` storing ``frame(data)`` (no size class) turns this red.
    """
    store = _store(f"frame-{uuid.uuid4().hex[:10]}")
    before = _metadata_log_bytes()
    for appended in range(1, 301):
        store.put("segments", "chunk", b"e" * (appended * 1000))
    grown = _metadata_log_bytes() - before
    assert store.get("segments", "chunk") == b"e" * 300_000
    assert grown < 1_200_000, f"metadata log grew {grown} bytes for 300 re-puts"
