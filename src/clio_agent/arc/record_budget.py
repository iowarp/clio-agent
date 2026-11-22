"""Keep one durable ARC record within what clio-core can place (take 1r).

A script that moves a file through a tool in base64 chunks puts megabytes into each
call's arguments and result. ARC's event log stored those verbatim, so one session's
event chunk reached 27 MB and clio-core refused it (``AsyncPutBlob`` code 12): the
turn could not be recorded and the session wedged in owner recovery.

This module bounds a record before it is stored:

* a string or bytes leaf longer than ``arc.record_leaf_max_bytes`` keeps its head and
  gains a typed marker naming its size and SHA-256. When an externalizer is wired
  (gact writes the leaf to the session's spill folder, deleted with the session), the
  marker also names the file that holds the whole value;
* a record still larger than ``arc.record_max_bytes`` after that is bounded again with
  a small leaf limit, so no single record dominates a chunk.

A bounded leaf stays a string, so readers that expect text still get text. What the
model reads is built elsewhere (the result's ``model_text``); this only bounds ARC's
copy. Values within the limits are returned unchanged (the same objects).
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
from collections.abc import Callable, Mapping
from typing import Any

from clio_agent import conf

logger = logging.getLogger(__name__)

#: Typed reason recorded when a leaf of a durable record is bounded.
RECORD_LEAF_BOUNDED_REASON = "arc_record_leaf_bounded"
#: Prefix of the marker that follows a bounded leaf's head.
BOUNDED_LEAF_MARKER = "[clio: "
#: Characters of a bounded leaf kept inline.
HEAD_CHARS = 2000
#: Leaf limit and kept head of the second pass, for a record that is still too large.
TIGHT_LEAF_BYTES = 4096
TIGHT_HEAD_CHARS = 200

#: ``(session_id, text, digest) -> path or None``: saves a bounded leaf's whole value.
Externalizer = Callable[[str, str, str], "str | None"]


def record_leaf_max_bytes() -> int:
    """Byte limit of one string/bytes leaf in a durable ARC record."""
    return conf.resolve(
        "arc.record_leaf_max_bytes",
        env="CLIO_ARC_RECORD_LEAF_MAX_BYTES",
        default=65_536,
        cast=conf.as_int,
    )


def record_max_bytes() -> int:
    """Byte limit of one whole durable ARC record (compact JSON)."""
    return conf.resolve(
        "arc.record_max_bytes",
        env="CLIO_ARC_RECORD_MAX_BYTES",
        default=1_048_576,
        cast=conf.as_int,
    )


def record_size(value: Any) -> int:
    """Compact JSON size of ``value`` in bytes (``repr`` for what JSON cannot encode)."""
    try:
        return len(
            json.dumps(value, separators=(",", ":"), ensure_ascii=False, default=repr).encode(
                "utf-8"
            )
        )
    except (TypeError, ValueError):
        return len(repr(value).encode("utf-8"))


def is_bounded_leaf(value: Any) -> bool:
    """Whether ``value`` is a leaf this module bounded."""
    return isinstance(value, str) and f"\n{BOUNDED_LEAF_MARKER}" in value


class _Bounder:
    def __init__(self, session_id: str, limit: int, externalize: Externalizer | None) -> None:
        self.session_id = session_id
        self.limit = limit
        self.externalize = externalize
        self.head = HEAD_CHARS
        self.bounded = 0

    def leaf(self, value: str | bytes) -> str | bytes:
        raw = value.encode("utf-8", "surrogatepass") if isinstance(value, str) else bytes(value)
        already = isinstance(value, str) and len(raw) < 16_384 and is_bounded_leaf(value)
        if len(raw) <= self.limit or already:
            return value
        digest = hashlib.sha256(raw).hexdigest()
        text = value if isinstance(value, str) else base64.b64encode(raw).decode("ascii")
        path: str | None = None
        if self.externalize is not None and self.session_id:
            try:
                path = self.externalize(self.session_id, text, digest)
            except Exception:  # noqa: BLE001 - the record is still bounded without the file
                logger.exception("reason=arc_record_externalize_failed session=%s", self.session_id)
        self.bounded += 1
        where = f"; full value saved at {path}" if path else "; full value not saved"
        kind = "text" if isinstance(value, str) else "binary data (saved as base64)"
        return (
            f"{text[: self.head]}\n{BOUNDED_LEAF_MARKER}{len(raw)} bytes of {kind} bounded in "
            f"this record (reason={RECORD_LEAF_BOUNDED_REASON}, sha256={digest}){where}]"
        )

    def walk(self, value: Any) -> Any:
        if isinstance(value, (str, bytes, bytearray)):
            return self.leaf(value)
        if isinstance(value, Mapping):
            out = {key: self.walk(item) for key, item in value.items()}
            return value if all(out[k] is value[k] for k in out) else out
        if isinstance(value, list):
            items = [self.walk(item) for item in value]
            return value if all(a is b for a, b in zip(items, value, strict=True)) else items
        if isinstance(value, tuple):
            items = [self.walk(item) for item in value]
            return value if all(a is b for a, b in zip(items, value, strict=True)) else tuple(items)
        return value


def bound_record(
    value: Any,
    *,
    session_id: str = "",
    externalize: Externalizer | None = None,
    leaf_max_bytes: int | None = None,
    max_bytes: int | None = None,
    label: str = "",
) -> Any:
    """``value`` with every oversized leaf bounded (module docstring); unchanged if it fits.

    Args:
        value: The record (a mapping, list or scalar).
        session_id: The owning session (names the externalized file's folder).
        externalize: Saves a bounded leaf's whole value; ``None`` keeps only the marker.
        leaf_max_bytes: Leaf limit; defaults to :func:`record_leaf_max_bytes`.
        max_bytes: Whole-record limit; defaults to :func:`record_max_bytes`.
        label: What the record is, for the log line.

    Returns:
        The bounded record.
    """
    leaf_limit = record_leaf_max_bytes() if leaf_max_bytes is None else leaf_max_bytes
    whole_limit = record_max_bytes() if max_bytes is None else max_bytes
    bounder = _Bounder(session_id, max(leaf_limit, HEAD_CHARS * 4), externalize)
    bounded = bounder.walk(value)
    if record_size(bounded) > whole_limit:
        bounder.limit = TIGHT_LEAF_BYTES
        bounder.head = TIGHT_HEAD_CHARS
        bounded = bounder.walk(bounded)
    if bounder.bounded:
        logger.info(
            "reason=%s session=%s record=%s leaves=%d bytes=%d",
            RECORD_LEAF_BOUNDED_REASON,
            session_id,
            label,
            bounder.bounded,
            record_size(bounded),
        )
    return bounded


__all__ = [
    "BOUNDED_LEAF_MARKER",
    "RECORD_LEAF_BOUNDED_REASON",
    "Externalizer",
    "bound_record",
    "is_bounded_leaf",
    "record_leaf_max_bytes",
    "record_max_bytes",
    "record_size",
]
