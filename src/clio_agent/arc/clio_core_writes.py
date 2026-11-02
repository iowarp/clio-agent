"""The write half of :class:`~clio_agent.arc.storage.ClioCoreStore` (owner module).

Three ways a record reaches clio-core, every one stall-guarded, async (GIL released
while awaited) and stream-audited for the ``segments`` kind:

* :meth:`ClioCoreWrites.put` -- a whole record, framed and padded to its size class
  (:func:`~clio_agent.arc.blob_frame.stored_body`), read back by ``get``;
* :meth:`ClioCoreWrites.put_at` -- raw bytes at an offset (no frame, no padding), the
  append-only chunk records (:mod:`clio_agent.arc.chunk_records`), read back by
  ``get_raw``;
* :meth:`ClioCoreWrites.put_many` -- independent records of either shape issued
  together.

Each carries the optional plain-text search companion at ``<name>.text`` (Thread D).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Optional

from clio_agent.arc.batch_put import BatchPutError, PutRecord
from clio_agent.arc.blob_frame import stored_body
from clio_agent.arc.clio_core_async_ops import store_delete, store_put, store_put_many
from clio_agent.arc.companion_policy import may_carry_companion
from clio_agent.arc.companion_write import (
    companion_body,
    companion_sizes,
    drop_shrinking,
    remember_written,
)
from clio_agent.arc.rpc_liveness import guarded_store_rpc
from clio_agent.runtime.stream_audit import stream_audit

__all__ = ["SEARCH_SUFFIX", "ClioCoreWrites"]

# Suffix for the optional plain-text companion blob (BM25 discovery, Thread D) a
# backend may store next to a record. NOT a record: scan()/get() skip it.
SEARCH_SUFFIX = ".text"


class ClioCoreWrites:
    """``put`` / ``put_at`` / ``put_many`` of :class:`~clio_agent.arc.storage.ClioCoreStore`."""

    _client: Any
    _tag_ids: Any

    def put(
        self,
        kind: str,
        name: str,
        data: bytes,
        *,
        tier: str = "warm",
        search_text: Optional[str] = None,
    ) -> None:
        # Bound each async RPC individually, with the GIL released while awaiting it.
        # base64-wrap: CTE GetBlob UTF-8-decodes, so store ascii-safe bytes; framed with
        # its length and padded to a size class so a growing record adds few blocks.
        payload = stored_body(data)
        if kind == "segments":  # #1339: live-lane audit evidence (one row per put)
            stream_audit("store.put", kind=kind, name=name, size=len(payload))
        guarded_store_rpc(self, "put", store_put, self, kind, name, payload, inline_kind=kind)
        self._put_companion(kind, name, search_text)
        # ``tier`` is advisory: the default single DRAM tier makes ReorganizeBlob a no-op.

    def put_at(
        self, kind: str, name: str, data: bytes, offset: int, *, search_text: Optional[str] = None
    ) -> None:
        """One raw put of ``data`` at ``offset``: no frame, no padding (a put past the end
        grows the blob; nothing shrinks it). The append-only chunk records
        (:mod:`clio_agent.arc.chunk_records`) are written this way; ``search_text`` is the
        record's companion exactly as :meth:`put` writes it."""
        if kind == "segments":  # #1339: live-lane audit evidence (one row per put)
            stream_audit("store.put", kind=kind, name=name, size=len(data), offset=offset)
        guarded_store_rpc(self, "put", store_put, self, kind, name, data, offset, inline_kind=kind)
        self._put_companion(kind, name, search_text)

    def _put_companion(self, kind: str, name: str, search_text: Optional[str]) -> None:
        """The optional BM25 companion (Thread D) at <name>.text; scan()/get() skip it."""
        companion = name + SEARCH_SUFFIX
        if search_text is not None:  # padded; a shrinking one is dropped first (MD9)
            pair = [(companion, companion_body(search_text))]
            guarded_store_rpc(self, "put", drop_shrinking, self, kind, pair)
            guarded_store_rpc(
                self, "put", store_put, self, kind, companion, pair[0][1], inline_kind=kind
            )
            remember_written(self, kind, pair)
        elif may_carry_companion(kind, name):  # #1334: never for the ``_events`` family
            guarded_store_rpc(self, "put", store_delete, self, kind, companion)  # a stale one
            companion_sizes(self).forget(kind, companion)

    def put_many(self, kind: str, records: Sequence[PutRecord]) -> None:
        """Put independent records concurrently on the async path (one stall-guarded
        batch: every ``AsyncPutBlob`` -- bodies and search companions -- is issued, then
        all are awaited). A record with an ``offset`` is a raw put there (:meth:`put_at`).
        A stale-companion probe runs only for a record that can carry one and has no text.

        Raises:
            BatchPutError: Some blob was lost after its bounded retry; the others of the
                batch were written (named in the error).
        """
        blobs: list[tuple[str, bytes]] = []
        companions: list[tuple[str, bytes]] = []
        offsets: dict[str, int] = {}
        stale: list[str] = []
        for record in records:
            raw = record.offset is not None
            payload = record.data if raw else stored_body(record.data)
            if kind == "segments":  # #1339: live-lane audit evidence (one row per put)
                at = {"offset": record.offset} if raw else {}
                stream_audit("store.put", kind=kind, name=record.name, size=len(payload), **at)
            blobs.append((record.name, payload))
            if record.offset:
                offsets[record.name] = record.offset
            if record.search_text is not None:
                companions.append((record.name + SEARCH_SUFFIX, companion_body(record.search_text)))
            elif may_carry_companion(kind, record.name):
                stale.append(record.name + SEARCH_SUFFIX)
        if companions:  # a shrinking companion is dropped before the batch (MD9)
            guarded_store_rpc(self, "put", drop_shrinking, self, kind, companions)
        lost = guarded_store_rpc(
            self, "put", store_put_many, self, kind, blobs + companions, offsets, inline_kind=kind
        )
        remember_written(self, kind, [pair for pair in companions if pair[0] not in lost])
        for companion in stale:
            guarded_store_rpc(self, "put", store_delete, self, kind, companion)
            companion_sizes(self).forget(kind, companion)
        if lost:
            written = [name for name, _ in blobs + companions if name not in lost]
            raise BatchPutError(kind, written, dict(lost))
