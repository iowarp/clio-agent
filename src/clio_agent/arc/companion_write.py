"""Writing ``.text`` search companions so a shrunk one never keeps searchable leftovers.

clio-core's ``PutBlob`` over an existing blob writes at offset 0 and does not always
shrink the blob (``blob_frame``'s module note), and the BM25 indexer tokenizes the
whole blob. Record bodies carry their own length, but a companion is plain text: a
document re-put with fewer words stayed findable by the words it lost (memory ledger
MD9). Two measures close that:

* Every companion is padded with spaces up to its :func:`~clio_agent.arc.blob_frame.size_class`.
  Whitespace never tokenizes, a re-put inside one class overwrites the whole blob, and
  a growing companion adds storage blocks only when it crosses a class, as bodies do.
* A companion whose stored blob is larger than its new padded size is deleted first:
  measured on iowarp-core 2.3.2, a put into a smaller class keeps the old tail.

Stored sizes are remembered per store for the companions this process wrote, so the
steady state costs no extra RPC; an unknown companion is probed once with
``GetBlobSize`` (about 1.3 ms). Another process writing the same companion makes the
remembered size stale; every memory document has one writer, the GACT server.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Sequence
from typing import Any

from clio_agent.arc.blob_frame import size_class
from clio_agent.arc.clio_core_async_ops import await_future, store_delete_many

_NOT_FOUND = 1  # GetBlobSize's published "no such blob" code
_REMEMBERED = 8192  # companion sizes kept per store


def companion_body(text: str) -> bytes:
    """The bytes stored for a companion: ``text`` padded with spaces to its size class."""
    body = text.encode("utf-8")
    return body + b" " * (size_class(len(body)) - len(body))


class CompanionSizes:
    """Stored sizes of the companions one store wrote, oldest forgotten first."""

    def __init__(self) -> None:
        self._sizes: OrderedDict[tuple[str, str], int] = OrderedDict()

    def get(self, kind: str, name: str) -> int | None:
        """The remembered stored size, or ``None`` when unknown."""
        return self._sizes.get((kind, name))

    def remember(self, kind: str, name: str, size: int) -> None:
        """Record that ``name`` now holds ``size`` bytes."""
        self._sizes[(kind, name)] = size
        self._sizes.move_to_end((kind, name))
        while len(self._sizes) > _REMEMBERED:
            self._sizes.popitem(last=False)

    def forget(self, kind: str, name: str) -> None:
        """Drop what is known about ``name`` (it was deleted)."""
        self._sizes.pop((kind, name), None)

    def clear(self) -> None:
        """Forget every size (the store was cleared)."""
        self._sizes.clear()


def companion_sizes(store: Any) -> CompanionSizes:
    """The companion sizes ``store`` remembers (created on first use)."""
    sizes = store.__dict__.get("_companion_sizes")
    if sizes is None:
        sizes = store.__dict__["_companion_sizes"] = CompanionSizes()
    return sizes


def _stored_sizes(store: Any, kind: str, names: Sequence[str]) -> dict[str, int]:
    """Probe the stored size of each name concurrently (0 when absent)."""
    tag_id = store._tag_ids.get(kind)
    local = store._cte.PoolQuery.Local()
    issued = [(name, store._client.AsyncGetBlobSize(tag_id, name, local)) for name in names]
    sizes: dict[str, int] = {}
    for name, future in issued:
        done = await_future(future, op_name="companion_size")
        code = done.wait(0)
        if code == _NOT_FOUND:
            sizes[name] = 0
        elif code != 0:
            raise RuntimeError(f"clio-core GetBlobSize failed for {name}: code {code}")
        else:
            sizes[name] = int(done.result(0))
    return sizes


def drop_shrinking(store: Any, kind: str, companions: Sequence[tuple[str, bytes]]) -> None:
    """Delete each companion whose stored blob is larger than its new body.

    Args:
        store: The ``ClioCoreStore`` about to write ``companions``.
        kind: Their record kind.
        companions: ``(blob name, new padded body)`` pairs.
    """
    sizes = companion_sizes(store)
    unknown = [name for name, _ in companions if sizes.get(kind, name) is None]
    probed = _stored_sizes(store, kind, unknown) if unknown else {}
    shrinking = []
    for name, body in companions:
        stored = sizes.get(kind, name)
        stored = probed.get(name, 0) if stored is None else stored
        if stored > len(body):
            shrinking.append(name)
    if shrinking:
        store_delete_many(store, kind, shrinking)


def remember_written(store: Any, kind: str, companions: Sequence[tuple[str, bytes]]) -> None:
    """Remember the sizes of companions that were just written."""
    sizes = companion_sizes(store)
    for name, body in companions:
        sizes.remember(kind, name, len(body))


__all__ = [
    "CompanionSizes",
    "companion_body",
    "companion_sizes",
    "drop_shrinking",
    "remember_written",
]
