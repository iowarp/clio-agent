"""Which CLIO wrote this clio-core store: the segment-list format check (owner module).

CLIO 0.9.5.1 and older stored every ARC segment list as one framed base64 blob
(``b"<n>:" + base64``, or bare base64 before the frame; :mod:`clio_agent.arc.blob_frame`).
This CLIO stores append-only records that start with
:data:`~clio_agent.arc.chunk_records.MAGIC` and has no reader for the old frame: reading
one fails typed ``arc_chunk_format_unsupported``. So an upgrade over such a store cannot
open the person's earlier conversations, memory or artifacts; the store is moved aside
(:mod:`clio_agent.gact.store_aside`) and CLIO starts with an empty one.

:func:`inspect_store` decides from the store itself, never from a version guess: one
name query for a bounded number of segment-list records, their sizes, then the first
bytes of the few smallest. The verdict is

* ``current``: every record read starts with the record magic;
* ``empty``: the store holds no segment list (a new store: nothing to open);
* ``old``: a record is in the retired frame (``arc_store_format_old``);
* ``unknown``: a record is neither, or the store could not be read
  (``arc_store_format_unknown``): the caller says so and asks, it never guesses.

The server checks once at boot (:mod:`clio_agent.gact.store_format_boot`); the
installers and ``clio-agent reset-store`` run the same check
(:mod:`clio_agent.gact.store_reset_command`).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Literal

from clio_agent.arc.chunk_records import MAGIC
from clio_agent.errors import ClioError

__all__ = [
    "ARC_STORE_FORMAT_OLD",
    "ARC_STORE_FORMAT_UNKNOWN",
    "LAST_OLD_FORMAT_RELEASE",
    "ArcStoreFormatOldError",
    "StoreFormat",
    "classify_head",
    "inspect_store",
]

logger = logging.getLogger(__name__)

#: Typed reason: the store holds segment lists in the format of CLIO 0.9.5.1 or older.
ARC_STORE_FORMAT_OLD = "arc_store_format_old"
#: Typed reason: which CLIO wrote the store could not be told (unreadable or unrecognized).
ARC_STORE_FORMAT_UNKNOWN = "arc_store_format_unknown"
#: The newest release whose store this CLIO cannot open.
LAST_OLD_FORMAT_RELEASE = "0.9.5.1"

#: Segment-list names one check asks clio-core for (one bounded name query).
_CANDIDATES = 32
#: Records whose first bytes one check reads: the smallest of the candidates.
_SAMPLE = 4
#: The first bytes of a record that decide its format.
_HEAD = 16

#: The retired frame: the base64 text's length, a colon, then the base64 text.
_OLD_FRAME = re.compile(rb"[0-9]{1,12}:[A-Za-z0-9+/=]")
#: A record stored before the frame: bare base64.
_BARE_BASE64 = re.compile(rb"[A-Za-z0-9+/]{4,}={0,2}")

Format = Literal["current", "old", "unrecognized"]
Verdict = Literal["current", "empty", "old", "unknown"]
#: What reading the store can fail with: clio-core refusals and typed blob errors.
_READ_ERRORS = (ClioError, OSError, RuntimeError, ValueError, TimeoutError)


def classify_head(head: bytes) -> Format:
    """Which format a segment-list record's first bytes are in (pure)."""
    if head.startswith(MAGIC):
        return "current"
    if _OLD_FRAME.match(head) or _BARE_BASE64.fullmatch(head[:_HEAD]):
        return "old"
    return "unrecognized"


@dataclass(frozen=True)
class StoreFormat:
    """The verdict of one check, with what it read (record names stay in the log).

    Attributes:
        verdict: ``current``, ``empty``, ``old`` or ``unknown`` (module docstring).
        reason: The typed reason of ``old`` / ``unknown``; empty otherwise.
        counts: Records read per format (``current``, ``old``, ``unrecognized``).
        error: Why the store could not be read, for ``unknown`` (a repr).
    """

    verdict: Verdict
    reason: str = ""
    counts: dict[str, int] = field(default_factory=dict)
    error: str = ""

    def as_dict(self) -> dict[str, Any]:
        """The verdict as JSON (command output, health details)."""
        out: dict[str, Any] = {"verdict": self.verdict, "records_read": dict(self.counts)}
        if self.reason:
            out["reason"] = self.reason
        if self.error:
            out["error"] = self.error
        return out


def _segment_names(store: Any) -> list[str]:
    """Up to :data:`_CANDIDATES` segment-list record names (one bounded name query)."""
    query = getattr(store, "query_names", None)
    if callable(query):  # clio-core: ask for a bounded number of names
        return sorted(query("segments", ".*", limit=_CANDIDATES))
    names: list[str] = []  # an in-process store: its listing is its own memory
    for name in store.scan_names("segments"):
        names.append(name)
        if len(names) >= _CANDIDATES:
            break
    return sorted(names)


def inspect_store(store: Any) -> StoreFormat:
    """Read the store's segment-list records and say which CLIO wrote them.

    Never raises for a read failure: an unreadable store is the ``unknown`` verdict
    with its error, so a caller reports it instead of guessing.
    """
    counts = {"current": 0, "old": 0, "unrecognized": 0}
    try:
        names = _segment_names(store)
        if not names:
            return StoreFormat("empty", counts=counts)
        sizes = store.sizes("segments", names)
        smallest = sorted((size, name) for name, size in sizes.items() if size > 0)[:_SAMPLE]
        for _size, name in smallest:
            raw = store.get_raw("segments", name)
            if raw is None:
                continue  # removed since it was listed
            kind = classify_head(bytes(raw[:_HEAD]))
            counts[kind] += 1
            if kind != "current":
                logger.warning("arc store format: record=%s format=%s", name, kind)
    except _READ_ERRORS as exc:
        logger.warning("arc store format: reason=%s error=%r", ARC_STORE_FORMAT_UNKNOWN, exc)
        return StoreFormat("unknown", ARC_STORE_FORMAT_UNKNOWN, counts, repr(exc))
    if counts["old"]:
        return StoreFormat("old", ARC_STORE_FORMAT_OLD, counts)
    if counts["unrecognized"]:
        return StoreFormat("unknown", ARC_STORE_FORMAT_UNKNOWN, counts, "unrecognized records")
    if not counts["current"]:
        # Names were listed but no record could be read back.
        return StoreFormat("unknown", ARC_STORE_FORMAT_UNKNOWN, counts, "no record read back")
    return StoreFormat("current", counts=counts)


class ArcStoreFormatOldError(ClioError):
    """The clio-core store was written by CLIO 0.9.5.1 or older; this CLIO cannot open it.

    Raised at boot before anything reads or writes the store, so the old data stays
    exactly as it was (nothing new is mixed into it) until it is moved aside.
    """

    def __init__(self, message: str, details: dict[str, Any]) -> None:
        super().__init__(message, error_type=ARC_STORE_FORMAT_OLD, details=details)
