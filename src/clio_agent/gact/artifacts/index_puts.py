"""The artifact index's puts, sent together when they are queued together.

A tool call's provenance transform writes three artifact-index records: the session's
transform page and the transform marked pending, both ahead of the call's
``artifact.transform.recorded`` event, and the transform again, confirmed, once the event
is recorded (``registry_store``). Each was its own clio-core round trip on the session's
history writer, one after another, ahead of the group commit; a script's nested calls paid
them per call (ledger CT5: 3.0 of the 7.3 records a nested call wrote).

The writer now hands each queued put here (:class:`IndexPut`) and sends a writer turn's
puts as one multi-put (:func:`put_batch`): the puts ahead of the turn's events in one, and
the confirms after them in another. Within a batch only the last put of a key is sent
(a session page rewritten by several calls is one record), and a page count (``~n``) goes
in a multi-put of its own before the pages it counts, as it was written before them.
The order between a turn's index puts, its events and its confirms is unchanged, so the
crash-safety the pending mark gives is the same.
"""

from __future__ import annotations

import contextvars
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

__all__ = ["INDEX_PUT_SINK", "IndexPut", "collecting", "put_batch"]

#: When set, a write-through index put is added here instead of sent (the writer's
#: after-event hooks, so a turn's confirms leave together).
INDEX_PUT_SINK: contextvars.ContextVar[list[IndexPut] | None] = contextvars.ContextVar(
    "artifact_index_put_sink", default=None
)


@dataclass(frozen=True)
class IndexPut:
    """One artifact-index put: callable on its own, or batched with others."""

    backend: Any
    key: str
    raw: bytes

    def __call__(self) -> None:
        self.backend._put(self.key, self.raw)


def put_batch(puts: list[IndexPut]) -> None:
    """Send ``puts`` as few multi-puts as their order allows (module doc)."""
    by_backend: dict[int, tuple[Any, dict[str, bytes]]] = {}
    for put in puts:
        _, latest = by_backend.setdefault(id(put.backend), (put.backend, {}))
        latest.pop(put.key, None)  # the last put of a key wins and keeps its place
        latest[put.key] = put.raw
    for backend, latest in by_backend.values():
        counts = [(key, raw) for key, raw in latest.items() if key.endswith("~n")]
        rest = [(key, raw) for key, raw in latest.items() if not key.endswith("~n")]
        for items in (counts, rest):
            if items:
                backend._put_many(items)


def collecting(sink: list[IndexPut], hook: Callable[[], None]) -> None:
    """Run ``hook`` with its write-through index puts added to ``sink``."""
    token = INDEX_PUT_SINK.set(sink)
    try:
        hook()
    finally:
        INDEX_PUT_SINK.reset(token)
