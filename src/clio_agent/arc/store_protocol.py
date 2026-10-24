"""The ``ARCStore`` protocol: the narrow persistence seam for ARC's record kinds.

``ClioCoreStore`` (:mod:`clio_agent.arc.storage`) is the one backend; the in-process
``_MemoryStore`` (:mod:`clio_agent.arc.live`) serves unit tests. Re-exported from
:mod:`clio_agent.arc.storage`, where callers have always imported it.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from typing import Optional, Protocol, runtime_checkable

from clio_agent.arc.batch_put import PutRecord


@runtime_checkable
class ARCStore(Protocol):
    """Narrow persistence seam for ARC's record kinds.

    A record is addressed by ``(kind, name)``: ``kind`` is one of :data:`ARC_KINDS`; ``name`` is the
    record stem (no extension). The store owns the physical layout and tiering, so ARC never touches
    the filesystem directly. clio-core maps ``(kind, name)`` onto namespaced, multi-tier
    storage.
    """

    def put(
        self,
        kind: str,
        name: str,
        data: bytes,
        *,
        tier: str = "warm",
        search_text: Optional[str] = None,
    ) -> None:
        """Persist ``data`` for ``(kind, name)`` (overwrites).

        ``search_text`` (optional) is a plain-text projection of the record for BM25 semantic
        discovery (Thread D); a backend may index it. ``None`` drops any existing companion.
        """
        ...

    def put_many(self, kind: str, records: Sequence[PutRecord]) -> None:
        """Put independent records together (no order between them).

        Every record is attempted; a store that lost any raises
        :class:`~clio_agent.arc.batch_put.BatchPutError` naming what was written.
        """
        ...

    def get(self, kind: str, name: str) -> Optional[bytes]:
        """Return bytes for ``(kind, name)`` or ``None`` if absent."""
        ...

    def exists(self, kind: str, name: str) -> bool:
        """Return whether a record exists for ``(kind, name)``."""
        ...

    def scan(self, kind: str, prefix: str = "") -> Iterator[tuple[str, bytes]]:
        """Yield ``(name, data)`` for every record in ``kind`` whose name
        starts with ``prefix`` (``""`` = all). Order is unspecified."""
        ...

    def scan_names(self, kind: str, prefix: str = "") -> Iterator[str]:
        """List matching record addresses without reading their bodies."""
        ...

    def delete(self, kind: str, name: str) -> None:
        """Delete the record for ``(kind, name)`` if present (no-op if absent)."""
        ...

    def clear(self) -> None:
        """Delete all persisted records across all kinds."""
        ...

    def supports_search(self) -> bool:
        """Whether :meth:`search` does real (e.g. BM25) semantic ranking."""
        ...

    def search(
        self,
        kind: str,
        query_text: str,
        *,
        name_prefix: str = "",
        name_re: str = "",
        k: int = 10,
    ) -> list[tuple[str, float]]:
        """Rank records in ``kind`` by relevance to ``query_text``: those whose name
        starts with ``name_prefix``, or whose whole name matches the regex ``name_re``
        (the two are exclusive). Returns ``[(name, score)]`` best-first, matches only."""
        ...
