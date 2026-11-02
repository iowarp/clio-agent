"""The append-only record format of an ARC segment-list blob in clio-core.

Every segment-list record of the ``segments`` kind (a plain scope, and every chunk of
the ``_events`` lane families) is stored in this format, so an append puts only the new
segments instead of re-encoding and re-putting the whole chunk:

``MAGIC`` (2 bytes) then records, then an end marker. A record is
``[u32 little-endian payload length][u32 crc32 of payload][payload]`` where the payload
is ``msgspec.msgpack.encode(Segment)``; the end marker is 8 zero bytes (length 0).

* **Append** is ONE put at the stored end offset of the new record(s) plus a fresh end
  marker. It overwrites the previous end marker, so a single put both appends and
  re-terminates the chunk (:func:`plan_write`).
* **Full rewrite** (a first write, an op that changes earlier segments, a shrinking
  rewrite, a migration) is one put at offset 0 of ``MAGIC``, every record and the end
  marker (:func:`plan_write` without a mark, :func:`full_body`). ``PutBlob`` never
  shrinks a blob; the end marker stops a reader before the stale tail, so a shrinking
  rewrite needs no zeroing.
* **Growth in size classes.** Every put that grows a clio-core blob adds a storage block
  and every later put logs the blob's whole block list to ``metadata.log``
  (:mod:`clio_agent.arc.blob_frame`). Measured on the test daemon: 300 appends of 1 KB
  at growing offsets wrote 3.6 MB of metadata log, one block per append. So a put that
  would grow the blob past its allocated size is zero-padded up to the next
  :func:`~clio_agent.arc.blob_frame.size_class` (whole pages, then about +25% steps):
  the blob gains a block only when it crosses a class (the same 300 appends: 0.36 MB),
  and the padding a chunk ever sends is about its final size. Zero padding sits past
  the end marker, so no reader ever sees it.
* **Read** (:func:`parse_chunk`) walks records until the end marker, a length that runs
  past the blob, or a CRC mismatch. A torn or partial trailing record (a lock-free
  reader that read mid-append, or a put that never completed) is the end of the chunk,
  not corruption; :func:`read_chunk` reports it as the ``arc_chunk_torn_tail`` trace
  event. A blob that does not start with ``MAGIC`` (the retired base64 frame) raises
  :class:`ChunkFormatUnsupportedError` (``arc_chunk_format_unsupported``): there is no
  compatibility reader, the CLIO state is reset instead.
* Records are raw msgpack, never base64, so a substring search over the committed
  bytes (``transcript_reads``, ``blob[:end]``) still finds stored text.

Writer invariant: ONE writer process per session. Within that process the chunk's
per-scope lock serializes appends (two threads appending to one chunk take turns), and
the cached end offset (:class:`ChunkMark`) is read and advanced under that lock. A
second writer process would compute its own end offset and overwrite records.

A failed or timed-out append may still land later (a pending ``PutBlob`` is never
retried, ``clio_core_async_ops``). The writer therefore abandons that chunk: its cache
entry is dropped and the chunk is sealed so the lane's next append rolls to a fresh
chunk (``lane_chunking.seal_chunk``) instead of writing at an offset the late put may
overwrite. Readers drop an exact duplicate record (same ``logical_time``, ``order`` and
``id``) when a chunk or a chunk family is read (:func:`dedupe`).
"""

from __future__ import annotations

import logging
import struct
import zlib
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any, Optional, Protocol

import msgspec

from clio_agent.arc.blob_frame import size_class
from clio_agent.arc.schema import Segment
from clio_agent.arc.segment_errors import SegmentReadError
from clio_agent.errors import ClioError
from clio_agent.runtime.stream_audit import stream_audit

__all__ = [
    "CHUNK_APPEND_ABANDONED",
    "CHUNK_FORMAT_UNSUPPORTED",
    "CHUNK_TORN_TAIL",
    "END",
    "MAGIC",
    "ChunkFormatUnsupportedError",
    "ChunkMark",
    "ChunkWrite",
    "ParsedChunk",
    "appendable",
    "dedupe",
    "encode_records",
    "full_body",
    "parse_chunk",
    "plan_write",
    "read_chunk",
    "read_segments",
]

logger = logging.getLogger(__name__)

#: The format byte pair: neither an ASCII digit (the retired ``<n>:`` frame) nor base64.
MAGIC = b"\xc1\x01"
#: The end marker: a record header of length 0 (no payload is ever empty).
END = b"\x00" * 8
_HEADER = struct.Struct("<II")
#: A record header can never declare more than this (a msgpack Segment far below it);
#: a larger length is a torn header, and a reader does not re-read toward it.
_MAX_RECORD = 1 << 30
#: Re-reads of a chunk whose size metadata lags its last append (see :func:`read_chunk`).
_LAG_REREADS = 3

CHUNK_FORMAT_UNSUPPORTED = "arc_chunk_format_unsupported"
CHUNK_TORN_TAIL = "arc_chunk_torn_tail"
CHUNK_APPEND_ABANDONED = "arc_chunk_append_abandoned"

_DECODER = msgspec.msgpack.Decoder(Segment)
_ENCODER = msgspec.msgpack.Encoder()


class ChunkFormatUnsupportedError(SegmentReadError):
    """A stored segment-list blob is not in the append-only record format.

    The pre-release base64-framed format is not read: the record is reported typed and
    the remedy is to reset the CLIO state. A :class:`SegmentReadError`, so every reader
    that already reports an unreadable scope reports this one too.
    """

    reason = CHUNK_FORMAT_UNSUPPORTED

    def __init__(self, name: str) -> None:
        """Name the record that cannot be read and the remedy."""
        self.name = name
        # SegmentReadError's own message names a scope; this one names the blob.
        ClioError.__init__(
            self,
            f"ARC record {name!r} is stored in a retired format this CLIO cannot read; "
            "reset the CLIO state (stop CLIO, remove its clio-core store, start again)",
            error_type=self.reason,
            details={"name": name, "recovery": "reset_clio_state"},
        )


@dataclass(frozen=True)
class ParsedChunk:
    """A chunk blob read back: its segments and where the next record goes.

    Attributes:
        segments: The records in stored order, exact duplicates dropped.
        end: Offset of the end marker (where the next append starts).
        terminated: The walk ended on an end marker (not on a torn tail).
        need: Bytes a reader must have to see the trailing record, when the walk ran
            past the blob (a lagging size); ``0`` when re-reading cannot help.
        blob: The bytes read (``blob[:end]`` is the committed record bytes).
    """

    segments: list[Segment]
    end: int
    terminated: bool
    need: int = 0
    blob: bytes = field(default=b"", repr=False)


@dataclass(frozen=True)
class ChunkMark:
    """The stored end of a cached chunk: valid only for the cached list it was made for.

    Attributes:
        end: Offset of the stored end marker.
        count: How many leading segments of ``segs`` that stored record holds.
        segs: The cached list object (identity is the validity check).
        alloc: Bytes the blob already holds (a put within them grows nothing).
    """

    end: int
    count: int
    segs: list[Segment] = field(repr=False)
    alloc: int = 0


@dataclass(frozen=True)
class ChunkWrite:
    """One planned put of a chunk: ``data`` at ``offset`` (see :func:`plan_write`).

    Attributes:
        offset: Where ``data`` goes (0 for a whole rewrite).
        data: The records, the end marker and any class padding.
        end: The end marker's offset once the put lands.
        alloc: The blob's size once the put lands (at least).
    """

    offset: int
    data: bytes
    end: int
    alloc: int

    def committed(self, segs: list[Segment]) -> ChunkMark:
        """The mark of the cached list ``segs`` once this put was accepted."""
        return ChunkMark(self.end, len(segs), segs, self.alloc)


class _RawStore(Protocol):
    def get_raw(self, kind: str, name: str, *, length: int = 0) -> Optional[bytes]: ...


def encode_records(segments: Iterable[Segment]) -> bytes:
    """The concatenated records of ``segments`` (no magic, no end marker).

    Raises:
        TypeError: A segment holds a value msgpack cannot encode.
    """
    out = bytearray()
    for segment in segments:
        payload = _ENCODER.encode(segment)
        out += _HEADER.pack(len(payload), zlib.crc32(payload))
        out += payload
    return bytes(out)


def plan_write(segments: Sequence[Segment], mark: Optional[ChunkMark]) -> ChunkWrite:
    """The one put that stores ``segments``: an append after ``mark``, else a rewrite.

    With ``mark`` (the stored record holds ``segments[:mark.count]``) the put is the new
    records plus a fresh end marker at ``mark.end``; without it, ``MAGIC``, every record
    and the end marker at offset 0. A put that ends past the blob's allocated size is
    zero-padded to the next size class (module docstring).
    """
    if mark is None:
        offset, body, alloc = 0, MAGIC + encode_records(segments), 0
    else:
        offset, body, alloc = mark.end, encode_records(segments[mark.count :]), mark.alloc
    end = offset + len(body)
    stop = end + len(END)
    if stop <= alloc:
        return ChunkWrite(offset, body + END, end, alloc)
    grown = size_class(stop)
    return ChunkWrite(offset, body + END + bytes(grown - stop), end, grown)


def full_body(segments: Sequence[Segment]) -> bytes:
    """The whole blob of ``segments``, put at offset 0 (a new chunk or a rewrite)."""
    return plan_write(segments, None).data


def appendable(mark: Optional[ChunkMark], cached: Any, staged: Sequence[Segment]) -> bool:
    """Whether ``staged`` only adds segments after what ``mark`` says is stored.

    True when the mark belongs to the cached list (same object) and every segment the
    stored record holds is, by identity, unchanged at the head of ``staged``. Committed
    segments are never mutated in place (an op stages a replaced copy), so identity is
    exact: a delete, summarize, replace or reorder fails it and is a full rewrite.
    """
    if mark is None or cached is not mark.segs or len(staged) < mark.count:
        return False
    if staged is mark.segs:
        return True
    return all(a is b for a, b in zip(staged, mark.segs[: mark.count], strict=False))


def dedupe(segments: Iterable[Segment]) -> list[Segment]:
    """Drop exact duplicate records (a late put that landed again), keeping the first.

    The key is ``(logical_time, order, id)``: a late landing repeats the whole record,
    so all three match, while two distinct segments never share an ``id``.
    """
    seen: set[tuple[int, float, str]] = set()
    out: list[Segment] = []
    for segment in segments:
        key = (segment.logical_time, segment.order, segment.id)
        if key not in seen:
            seen.add(key)
            out.append(segment)
    return out


def parse_chunk(name: str, blob: bytes, *, decode: bool = True) -> ParsedChunk:
    """Walk the records of a chunk blob (pure; no I/O, no trace event).

    ``decode=False`` checks every record's length and checksum but decodes none (a
    reader that only needs the committed bytes); ``segments`` is then empty.

    Raises:
        ChunkFormatUnsupportedError: The blob does not start with :data:`MAGIC`.
        msgspec.DecodeError: A record whose CRC matches does not decode as a Segment
            (corruption the checksum cannot explain; the caller reports it typed).
    """
    if blob[: len(MAGIC)] != MAGIC:
        raise ChunkFormatUnsupportedError(name)
    view = memoryview(blob)
    size = len(blob)
    pos = len(MAGIC)
    segments: list[Segment] = []

    def done(terminated: bool, need: int = 0) -> ParsedChunk:
        return ParsedChunk(dedupe(segments), pos, terminated, need, blob)

    while True:
        if pos + _HEADER.size > size:
            return done(False, pos + _HEADER.size)
        length, crc = _HEADER.unpack_from(view, pos)
        if length == 0:
            return done(True)
        stop = pos + _HEADER.size + length
        if length > _MAX_RECORD:
            return done(False)
        if stop > size:
            return done(False, stop)
        payload = view[pos + _HEADER.size : stop]
        if zlib.crc32(payload) != crc:
            return done(False)
        if decode:
            segments.append(_DECODER.decode(payload))
        pos = stop


def read_chunk(
    store: _RawStore, kind: str, name: str, *, decode: bool = True
) -> Optional[ParsedChunk]:
    """Read and parse one chunk blob (``None`` when it is not stored).

    clio-core's size metadata can lag the last put (``blob_frame.read_framed_blob``):
    a walk that runs past the reported size is read again, up to the bytes its
    trailing record needs, a bounded number of times. A tail still torn after that is
    the end of the chunk, reported as the ``arc_chunk_torn_tail`` trace event.

    Raises:
        ChunkFormatUnsupportedError: The blob is in the retired format.
        msgspec.DecodeError: A checksummed record does not decode.
    """
    raw = store.get_raw(kind, name)
    if raw is None:
        return None
    parsed = parse_chunk(name, raw, decode=decode)
    reported = len(raw)
    for _ in range(_LAG_REREADS):
        if parsed.terminated or parsed.need <= len(raw):
            break
        again = store.get_raw(kind, name, length=parsed.need)
        if again is None or len(again) <= len(raw):
            break
        raw = again
        parsed = parse_chunk(name, raw, decode=decode)
    if len(raw) > reported and parsed.terminated:  # as blob_frame reports the same lag
        logger.warning(
            "reason=clio_core_blob_size_stale name=%s reported=%s read=%s",
            name,
            reported,
            len(raw),
        )
    if not parsed.terminated:
        stream_audit(CHUNK_TORN_TAIL, name=name, end=parsed.end, stored=len(raw))
        logger.info(
            "reason=%s name=%s end=%d stored=%d", CHUNK_TORN_TAIL, name, parsed.end, len(raw)
        )
    return parsed


def read_segments(store: _RawStore, session_id: str, scope: str, name: str) -> list[Segment]:
    """A stored chunk's segments (``[]`` when absent), every failure typed.

    Raises:
        SegmentReadError: The record does not decode (:class:`ChunkFormatUnsupportedError`
            for the retired format).
    """
    try:
        parsed = read_chunk(store, "segments", name)
    except msgspec.DecodeError as exc:
        raise SegmentReadError(session_id, scope) from exc
    return parsed.segments if parsed is not None else []
