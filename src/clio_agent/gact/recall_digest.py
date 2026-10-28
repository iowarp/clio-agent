"""The search digest of one conversation: what recall and reference search read of it.

Recall (``session_search``, ``GET /v1/session-recall/search``) and the Evidence board's
``plan`` / ``evidence_source`` references look at a few fields of a few part types of
every message. Assembling whole transcripts for that read every message-part atom of
every conversation per request. A :class:`TranscriptDigest` is the projection those
searches need, computed from a session's assembled messages by ONE function
(:func:`digest_messages`), so a digest read from the clio-core index
(:mod:`clio_agent.gact.recall_index`) and one computed on the spot from a transcript
are the same value and produce the same hits:

* ``parts``: every ``text`` / ``thinking`` / ``error`` part with non-blank text (the
  text stripped), with the identity and times a recall hit carries;
* ``plans``: every ``plan`` part and compaction record, as the Evidence board's plan
  payload (without the session id, which the reader adds);
* ``sources``: every referenceable source a part names (a ``resource_link`` URI, a
  web URL in its metadata or content blocks), first occurrence per value.

The digest is stored compressed (msgpack + zlib) under a format version; a record of
another version reads as absent, so a format change rebuilds rather than misreads.
"""

from __future__ import annotations

import zlib
from collections.abc import Iterable
from typing import TYPE_CHECKING, Optional

import msgspec

if TYPE_CHECKING:
    from clio_agent.gact.types import Message

__all__ = [
    "DIGEST_VERSION",
    "SEARCHABLE_PART_TYPES",
    "PlanEntry",
    "SearchPart",
    "SourceEntry",
    "TranscriptDigest",
    "decode_digest",
    "digest_messages",
    "encode_digest",
]

#: Bumped on any change to what :func:`digest_messages` keeps; older records rebuild.
DIGEST_VERSION = 1
#: Part types recall searches (the transcript text a person or model wrote).
SEARCHABLE_PART_TYPES: frozenset[str] = frozenset({"text", "thinking", "error"})


class SearchPart(msgspec.Struct, array_like=True, frozen=True):
    """One searchable part with its message's identity and times."""

    message_id: str
    part_id: str
    role: str
    created_at: str
    updated_at: str
    text: str


class PlanEntry(msgspec.Struct, array_like=True, frozen=True):
    """One plan part or compaction record (the Evidence board's plan payload)."""

    message_id: str
    part_id: str
    title: str
    detail: str
    type: str


class SourceEntry(msgspec.Struct, array_like=True, frozen=True):
    """One referenceable source named by a part (first occurrence of its value)."""

    message_id: str
    source_id: str
    label: str
    value: str


class TranscriptDigest(msgspec.Struct, frozen=True):
    """What recall and reference search read of one conversation."""

    parts: tuple[SearchPart, ...] = ()
    plans: tuple[PlanEntry, ...] = ()
    sources: tuple[SourceEntry, ...] = ()


class _Stored(msgspec.Struct, frozen=True):
    version: int
    digest: TranscriptDigest


_ENCODER = msgspec.msgpack.Encoder()
_DECODER = msgspec.msgpack.Decoder(_Stored)


def digest_messages(messages: Iterable["Message"]) -> TranscriptDigest:
    """The digest of an assembled transcript (the one projection every reader uses)."""

    from clio_agent.gact.context_reference_evidence import (  # noqa: PLC0415 - cycle
        part_source_candidates,
    )
    from clio_agent.gact.summarization_record import as_summarization  # noqa: PLC0415

    parts: list[SearchPart] = []
    plans: list[PlanEntry] = []
    sources: list[SourceEntry] = []
    seen_values: set[str] = set()
    for message in messages:
        for index, part in enumerate(message.parts):
            if part.type in SEARCHABLE_PART_TYPES:
                text = part.text.strip()
                if text:
                    parts.append(
                        SearchPart(
                            message.id,
                            part.id,
                            message.role,
                            message.created_at,
                            message.updated_at,
                            text,
                        )
                    )
            record = as_summarization(part)
            if part.type == "plan" or record is not None:
                plans.append(
                    PlanEntry(
                        message.id,
                        part.id or str(index),
                        part.title or ("Compacted context" if record is not None else "Plan"),
                        record.text if record is not None else (part.summary or part.text),
                        "summarization" if record is not None else part.type,
                    )
                )
            for source_id, label, value in part_source_candidates(part, index):
                if value in seen_values:
                    continue
                seen_values.add(value)
                sources.append(SourceEntry(message.id, source_id, label, value))
    return TranscriptDigest(tuple(parts), tuple(plans), tuple(sources))


def encode_digest(digest: TranscriptDigest) -> bytes:
    """The stored record bytes of ``digest`` (versioned, compressed)."""

    return zlib.compress(_ENCODER.encode(_Stored(DIGEST_VERSION, digest)), 1)


def decode_digest(data: bytes) -> Optional[TranscriptDigest]:
    """The digest a record holds; ``None`` for another format version.

    Raises:
        zlib.error, msgspec.DecodeError: The record is not a digest (the caller types it).
    """

    stored = _DECODER.decode(zlib.decompress(data))
    return stored.digest if stored.version == DIGEST_VERSION else None
