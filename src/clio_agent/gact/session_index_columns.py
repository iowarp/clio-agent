"""The session index's columns and how a bucket of them is stored (owner module).

:mod:`~clio_agent.gact.session_records` keeps one index row per session in
:data:`~clio_agent.gact.session_records.INDEX_BUCKETS` bucket records. Every CLIO since
session records reads a bucket ``index/<nn>`` as ``{id: [values]}`` and builds its row
positionally from exactly the :data:`BASE_FIELDS` (workspace, parent, kind, archive,
times, status, title). Those records keep that layout, so an older CLIO opening a store
this one wrote reads it as before.

The columns added since (the counters ``/v1/metrics`` sums, the flags the autonomy
controls, the gate replay, a provider swap and the memory tick select by:
:data:`EXTENSION_FIELDS`) live beside it in ``columns/<nn>``, which an older CLIO never
reads. That record names its fields and carries the checksum of the ``index/<nn>`` body
it was written with. An older CLIO that rewrites ``index/<nn>`` (a downgrade, then an
upgrade again) leaves the checksum stale, so every row of that bucket loads *unfilled*,
as does a row missing from ``columns/<nn>`` (indexed before the columns existed). The
``session_record_columns`` store migration fills unfilled rows from their records, and
is owed again while any row is unfilled (:func:`unfilled_in_store`).

What the checksum cannot see: an older CLIO change that moved no base column (a
session's selected model or memory metadata changed without its status, title or
recency moving) leaves that session's extension columns as they were until its next
write here.
"""

from __future__ import annotations

import json
import zlib
from collections.abc import Iterable, Mapping
from dataclasses import astuple, dataclass, fields, replace
from typing import Any

from clio_agent.gact.providers.config import _model_ref_is_empty
from clio_agent.gact.session_kind import session_kind

#: The prefixes of a bucket's two records (``index/<nn>`` and ``columns/<nn>``).
BASE_PREFIX = "index/"
EXTENSION_PREFIX = "columns/"
#: Typed reason of a ``columns/<nn>`` record that does not match its ``index/<nn>``.
EXTENSION_STALE_REASON = "session_index_columns_stale"


@dataclass(frozen=True)
class RecordColumns:
    """The indexed facts of one session (everything else lives in its record)."""

    id: str
    workspace_id: str
    parent_session_id: str
    kind: str
    archived: bool
    created_at: str
    recent_at: str
    interaction_known: bool
    status: str = ""
    title: str = ""
    # ---- extension columns (``columns/<nn>``; an older CLIO does not read them) ----
    # Counters the activity metrics sum.
    message_count: int = 0
    tokens_input: int = 0
    tokens_output: int = 0
    cost_usd: float = 0.0
    #: An active goal or loop (``metadata.goal`` / ``metadata.loop``).
    autonomous: bool = False
    #: An execution gate is open (``metadata.execution_gate``).
    gated: bool = False
    #: A background memory pass is in flight or has ended (``metadata.memory_pass`` /
    #: ``metadata.memory_last_pass``).
    memory_runs: bool = False
    #: The session selected its own model (``session.model``).
    model_selected: bool = False
    #: A background memory pass is in flight (``metadata.memory_pass``).
    memory_pass_open: bool = False
    #: The memory skip the record settles until it changes (``memory.tick_index``).
    memory_settled: str = ""


#: The columns stored positionally in ``index/<nn>``: never reorder or extend them.
BASE_FIELDS: tuple[str, ...] = (
    "id",
    "workspace_id",
    "parent_session_id",
    "kind",
    "archived",
    "created_at",
    "recent_at",
    "interaction_known",
    "status",
    "title",
)
#: The columns stored by name in ``columns/<nn>``.
EXTENSION_FIELDS: tuple[str, ...] = tuple(
    item.name for item in fields(RecordColumns) if item.name not in BASE_FIELDS
)
_BASE_WIDTH = len(BASE_FIELDS)


def base_values(row: RecordColumns) -> tuple[Any, ...]:
    """The :data:`BASE_FIELDS` of ``row``, in their stored order."""
    return astuple(row)[:_BASE_WIDTH]


def extension_values(row: RecordColumns) -> tuple[Any, ...]:
    """The :data:`EXTENSION_FIELDS` of ``row``, in their stored order."""
    return astuple(row)[_BASE_WIDTH:]


def extension_name(bucket: str) -> str:
    """The ``columns/<nn>`` record beside the ``index/<nn>`` bucket ``bucket``."""
    return EXTENSION_PREFIX + bucket.removeprefix(BASE_PREFIX)


def encode_bucket(
    bucket: str, rows: Iterable[RecordColumns], unfilled: frozenset[str] | set[str]
) -> dict[str, bytes]:
    """Both records of ``bucket`` for ``rows`` (``unfilled`` rows stay out of the
    extension, so they load unfilled again until the migration fills them)."""

    ordered = sorted(rows, key=lambda row: row.id)
    base = json.dumps(
        {row.id: list(base_values(row)) for row in ordered}, separators=(",", ":")
    ).encode()
    extension = {
        "base": zlib.crc32(base),
        "fields": list(EXTENSION_FIELDS),
        "rows": {row.id: list(extension_values(row)) for row in ordered if row.id not in unfilled},
    }
    return {bucket: base, extension_name(bucket): json.dumps(extension).encode()}


def decode_bucket(
    base: bytes | None, extension: bytes | None
) -> tuple[list[RecordColumns], set[str]]:
    """The rows of one stored bucket and the ids whose extension columns are unfilled."""

    if not base:
        return [], set()
    filled = _extension_rows(base, extension)
    rows: list[RecordColumns] = []
    unfilled: set[str] = set()
    for sid, values in json.loads(base).items():
        row = RecordColumns(*values)  # positional, as every CLIO reads ``index/<nn>``
        found = filled.get(sid)
        if found is None:
            unfilled.add(sid)
        else:
            row = replace(row, **found)
        rows.append(row)
    return rows, unfilled


def _extension_rows(base: bytes, extension: bytes | None) -> dict[str, dict[str, Any]]:
    if not extension:
        return {}
    body = json.loads(extension)
    if not isinstance(body, Mapping) or body.get("base") != zlib.crc32(base):
        return {}  # written for another base body: an older CLIO rewrote the bucket
    names = [str(name) for name in body.get("fields") or ()]
    known = set(EXTENSION_FIELDS)
    return {
        str(sid): {name: value for name, value in zip(names, values, strict=True) if name in known}
        for sid, values in (body.get("rows") or {}).items()
        if isinstance(values, list) and len(values) == len(names)  # else: unfilled
    }


def unfilled_in_store(store: Any, kind: str, buckets: int) -> int:
    """How many stored index rows are unfilled, from the bucket records alone."""
    count = 0
    for number in range(buckets):
        base = store.get(kind, f"{BASE_PREFIX}{number:02d}")
        extension = store.get(kind, f"{EXTENSION_PREFIX}{number:02d}")
        count += len(decode_bucket(base, extension)[1])
    return count


def record_columns(session: Any) -> RecordColumns:
    """The indexed columns of ``session`` (the views' filters and recency)."""

    from clio_agent.gact.memory.tick_index import settled_reason  # noqa: PLC0415 - import cycle

    metadata = session.metadata if isinstance(session.metadata, Mapping) else {}
    return RecordColumns(
        id=session.id,
        workspace_id=session.workspace_id,
        parent_session_id=session.parent_session_id,
        kind=session_kind(session),
        archived=bool(session.archived),
        created_at=session.created_at,
        # The recency the wire shows (protocol.v3.session.session_to_v3).
        recent_at=session.last_interaction_at or session.created_at,
        interaction_known=bool(session.last_interaction_at),
        status=str(session.status or ""),
        title=str(session.title or ""),
        message_count=int(session.message_count or 0),
        tokens_input=int(session.tokens_input or 0),
        tokens_output=int(session.tokens_output or 0),
        cost_usd=float(session.cost_usd or 0.0),
        autonomous=_flag(metadata, "goal", "active") or _flag(metadata, "loop", "active"),
        gated=bool(metadata.get("execution_gate")),
        memory_runs=bool(metadata.get("memory_pass") or metadata.get("memory_last_pass")),
        model_selected=not _model_ref_is_empty(session.model),
        memory_pass_open=bool(metadata.get("memory_pass")),
        memory_settled=settled_reason(session),
    )


def _flag(metadata: Mapping[str, Any], key: str, field: str) -> bool:
    value = metadata.get(key)
    return bool(isinstance(value, Mapping) and value.get(field))


__all__ = [
    "BASE_FIELDS",
    "EXTENSION_FIELDS",
    "EXTENSION_STALE_REASON",
    "RecordColumns",
    "base_values",
    "decode_bucket",
    "encode_bucket",
    "extension_name",
    "extension_values",
    "record_columns",
    "unfilled_in_store",
]
