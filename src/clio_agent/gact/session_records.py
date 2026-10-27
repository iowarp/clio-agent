"""Session metadata records: one small row per session, with a recent-first index.

:class:`~clio_agent.gact.sessions.SessionStore` used to keep every session's
metadata resident and rewrite the whole ``sessions.json`` on every change, so a
store after months of flow-heavy use paid O(sessions) per write and held every
row in memory from boot. This module owns the replacement table:

* **one row per session** in ``sessions.sqlite3`` next to the legacy
  ``sessions.json`` -- a change to one session writes that one row;
* **indexed columns** for what the conversation list filters and sorts on
  (workspace, archive bucket, session kind, parent, recency), so the rail reads a
  page by range (:meth:`SessionRecords.page`) without decoding any other row;
* **a one-time import** of a legacy ``sessions.json`` (recorded in the ``meta``
  table so it never runs twice); the JSON file is left in place untouched.

The rail order is the recency the wire shows (``last_interaction_at``, else
``created_at`` -- :func:`clio_agent.gact.protocol.v3.session.session_to_v3`),
newest first, ties broken by id. Page cursors are opaque and keyset-based: a
cursor names the last row of the previous page, never an offset.
"""

from __future__ import annotations

import base64
import json
import logging
import sqlite3
import threading
import weakref
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from clio_agent import conf
from clio_agent.gact.session_kind import session_kind
from clio_agent.runtime.stream_audit import stream_audit

logger = logging.getLogger(__name__)

#: Typed reasons this module reports (no silent fallback).
LEGACY_IMPORTED_REASON = "session_records_legacy_imported"
LEGACY_UNREADABLE_REASON = "session_records_legacy_unreadable"
ROW_UNREADABLE_REASON = "session_record_unreadable"

_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS sessions (
        id TEXT PRIMARY KEY,
        workspace_id TEXT NOT NULL,
        parent_session_id TEXT NOT NULL,
        kind TEXT NOT NULL,
        archived INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        recent_at TEXT NOT NULL,
        interaction_known INTEGER NOT NULL,
        body TEXT NOT NULL
    )""",
    "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    "CREATE INDEX IF NOT EXISTS sessions_recent ON sessions(recent_at DESC, id DESC)",
    """CREATE INDEX IF NOT EXISTS sessions_workspace_recent
        ON sessions(workspace_id, archived, recent_at DESC, id DESC)""",
    "CREATE INDEX IF NOT EXISTS sessions_created ON sessions(created_at DESC, id DESC)",
    "CREATE INDEX IF NOT EXISTS sessions_parent ON sessions(parent_session_id)",
)
_LEGACY_IMPORT_KEY = "legacy_json_import"


class InvalidCursorError(ValueError):
    """A page cursor that this server did not issue (or that was altered)."""

    reason = "invalid_cursor"


@dataclass(frozen=True)
class RecordColumns:
    """The indexed facts of one session row (everything else lives in ``body``)."""

    id: str
    workspace_id: str
    parent_session_id: str
    kind: str
    archived: bool
    created_at: str
    recent_at: str
    interaction_known: bool


@dataclass(frozen=True)
class PageQuery:
    """One rail page request; ``None`` filters are not applied."""

    workspace_id: Optional[str] = None
    archived: Optional[bool] = False
    kinds: Optional[frozenset[str]] = None
    parent_session_id: Optional[str] = None
    limit: int = 50
    cursor: Optional[str] = None


def encode_cursor(recent_at: str, session_id: str) -> str:
    """The opaque keyset cursor naming the last row of a page."""

    raw = json.dumps([recent_at, session_id], separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str) -> tuple[str, str]:
    """Decode a cursor from :func:`encode_cursor`; anything else is refused."""

    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        value = json.loads(base64.urlsafe_b64decode(padded.encode()))
    except (ValueError, TypeError) as exc:
        raise InvalidCursorError(f"invalid session page cursor: {cursor!r}") from exc
    if (
        not isinstance(value, list)
        or len(value) != 2
        or not all(isinstance(part, str) for part in value)
    ):
        raise InvalidCursorError(f"invalid session page cursor: {cursor!r}")
    return value[0], value[1]


class SessionRecords:
    """The SQLite table of session rows. Callers serialize access (one lock).

    ``path=None`` keeps the table in memory (tests and ephemeral servers).
    """

    def __init__(self, path: Optional[Path], *, legacy_json: Optional[Path] = None) -> None:
        self.path = path
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
        target = ":memory:" if path is None else str(path)
        self._db = sqlite3.connect(target, timeout=30, check_same_thread=False)
        self._db.row_factory = None
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")
        self._io = threading.Lock()  # the connection is shared across threads
        # Close the connection with its owner, never leaving it to the collector.
        self._closer = weakref.finalize(self, self._db.close)
        with self._io, self._db:
            for statement in _SCHEMA:
                self._db.execute(statement)
        if legacy_json is not None:
            self._import_legacy(legacy_json)

    # ---- legacy import ----------------------------------------------------

    def _import_legacy(self, legacy_json: Path) -> None:
        """Import ``sessions.json`` once; the marker makes every later boot skip it."""

        with self._io:
            done = self._db.execute(
                "SELECT value FROM meta WHERE key=?", (_LEGACY_IMPORT_KEY,)
            ).fetchone()
        if done is not None or not legacy_json.is_file():
            return
        try:
            raw = json.loads(legacy_json.read_text())
        except (json.JSONDecodeError, OSError) as exc:
            # The file is kept for out-of-band salvage; nothing is marked done, so a
            # repaired file is imported on the next start.
            stream_audit(
                LEGACY_UNREADABLE_REASON,
                reason=LEGACY_UNREADABLE_REASON,
                path=str(legacy_json),
                error=type(exc).__name__,
            )
            logger.warning("session records: legacy %s unreadable (%s)", legacy_json, exc)
            return
        payloads = raw.values() if isinstance(raw, dict) else ()
        from clio_agent.gact.sessions import Session  # noqa: PLC0415 - import cycle

        rows: list[tuple[RecordColumns, str]] = []
        for payload in payloads:
            if not isinstance(payload, dict):
                continue
            try:
                session = Session(**payload)
            except TypeError:
                continue  # schema drift: the same per-row skip the JSON loader made
            rows.append((record_columns(session), json.dumps(payload, sort_keys=True)))
        with self._io, self._db:
            self._db.executemany(
                "INSERT OR IGNORE INTO sessions VALUES (?,?,?,?,?,?,?,?,?)",
                [_row_values(columns, body) for columns, body in rows],
            )
            self._db.execute(
                "INSERT OR REPLACE INTO meta VALUES (?, ?)", (_LEGACY_IMPORT_KEY, str(len(rows)))
            )
        stream_audit(
            LEGACY_IMPORTED_REASON,
            reason=LEGACY_IMPORTED_REASON,
            path=str(legacy_json),
            sessions=len(rows),
        )

    # ---- rows -------------------------------------------------------------

    def put(self, columns: RecordColumns, body: str) -> None:
        """Write one row (insert or replace) and commit it."""

        with self._io, self._db:
            self._db.execute(
                "INSERT OR REPLACE INTO sessions VALUES (?,?,?,?,?,?,?,?,?)",
                _row_values(columns, body),
            )

    def delete(self, session_id: str) -> None:
        """Remove one row."""

        with self._io, self._db:
            self._db.execute("DELETE FROM sessions WHERE id=?", (session_id,))

    def body(self, session_id: str) -> Optional[str]:
        """One row's stored body, or ``None``."""

        with self._io:
            row = self._db.execute("SELECT body FROM sessions WHERE id=?", (session_id,)).fetchone()
        return None if row is None else str(row[0])

    def bodies(self, *, workspace_id: Optional[str] = None) -> list[tuple[str, str]]:
        """Every ``(id, body)``, newest created first (the historical list order)."""

        sql = "SELECT id, body FROM sessions"
        args: tuple[Any, ...] = ()
        if workspace_id:
            sql += " WHERE workspace_id=?"
            args = (workspace_id,)
        sql += " ORDER BY created_at DESC, id DESC"
        with self._io:
            return [(str(i), str(b)) for i, b in self._db.execute(sql, args).fetchall()]

    def bodies_for(self, ids: Sequence[str]) -> dict[str, str]:
        """The bodies of the named rows (missing ids are absent)."""

        out: dict[str, str] = {}
        for start in range(0, len(ids), 500):
            chunk = list(ids[start : start + 500])
            marks = ",".join("?" * len(chunk))
            with self._io:
                rows = self._db.execute(
                    f"SELECT id, body FROM sessions WHERE id IN ({marks})",  # noqa: S608 - placeholders only
                    chunk,
                ).fetchall()
            out.update((str(i), str(b)) for i, b in rows)
        return out

    def ids(self) -> list[str]:
        """Every session id (no body is read)."""

        with self._io:
            return [str(row[0]) for row in self._db.execute("SELECT id FROM sessions")]

    def count(self) -> int:
        """How many sessions exist."""

        with self._io:
            return int(self._db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0])

    def child_ids_by_parent(self) -> dict[str, list[str]]:
        """``{parent_session_id: [child ids]}`` from the parent column alone."""

        with self._io:
            rows = self._db.execute(
                "SELECT parent_session_id, id FROM sessions WHERE parent_session_id != ''"
                " ORDER BY created_at DESC, id DESC"
            ).fetchall()
        index: dict[str, list[str]] = {}
        for parent, child in rows:
            index.setdefault(str(parent), []).append(str(child))
        return index

    def most_recent_id(self) -> Optional[str]:
        """The newest created session's id."""

        with self._io:
            row = self._db.execute(
                "SELECT id FROM sessions ORDER BY created_at DESC, id DESC LIMIT 1"
            ).fetchone()
        return None if row is None else str(row[0])

    def without_interaction_time(self) -> list[str]:
        """Ids of rows written before ``last_interaction_at`` existed and still lacking it."""

        with self._io:
            return [
                str(row[0])
                for row in self._db.execute(
                    "SELECT id FROM sessions WHERE interaction_known=0 ORDER BY id"
                )
            ]

    # ---- the rail page ----------------------------------------------------

    def page(self, query: PageQuery) -> tuple[list[tuple[str, str]], Optional[str], int]:
        """One page of ``(id, body)`` newest-interaction first, its next cursor, and the total.

        ``total`` counts every row matching the filters (all pages), so a client can
        say how many conversations exist without loading them.
        """

        where: list[str] = []
        args: list[Any] = []
        if query.workspace_id:
            where.append("workspace_id=?")
            args.append(query.workspace_id)
        if query.archived is not None:
            where.append("archived=?")
            args.append(1 if query.archived else 0)
        if query.kinds:
            kinds = sorted(query.kinds)
            where.append(f"kind IN ({','.join('?' * len(kinds))})")
            args.extend(kinds)
        if query.parent_session_id:
            where.append("parent_session_id=?")
            args.append(query.parent_session_id)
        filters = (" WHERE " + " AND ".join(where)) if where else ""
        keyset = list(where)
        keyset_args = list(args)
        if query.cursor:
            recent_at, last_id = decode_cursor(query.cursor)
            keyset.append("(recent_at < ? OR (recent_at = ? AND id < ?))")
            keyset_args.extend([recent_at, recent_at, last_id])
        page_filter = (" WHERE " + " AND ".join(keyset)) if keyset else ""
        limit = max(1, int(query.limit))
        with self._io:
            total = int(
                self._db.execute(
                    f"SELECT COUNT(*) FROM sessions{filters}",  # noqa: S608 - placeholders only
                    args,
                ).fetchone()[0]
            )
            rows = self._db.execute(
                f"SELECT id, recent_at, body FROM sessions{page_filter}"  # noqa: S608
                " ORDER BY recent_at DESC, id DESC LIMIT ?",
                [*keyset_args, limit + 1],
            ).fetchall()
        more = len(rows) > limit
        rows = rows[:limit]
        next_cursor = encode_cursor(str(rows[-1][1]), str(rows[-1][0])) if more and rows else None
        return [(str(i), str(b)) for i, _r, b in rows], next_cursor, total

    def close(self) -> None:
        """Close the connection (idempotent)."""

        with self._io:
            self._closer()


def _row_values(columns: RecordColumns, body: str) -> tuple[Any, ...]:
    return (
        columns.id,
        columns.workspace_id,
        columns.parent_session_id,
        columns.kind,
        1 if columns.archived else 0,
        columns.created_at,
        columns.recent_at,
        1 if columns.interaction_known else 0,
        body,
    )


def record_columns(session: Any) -> RecordColumns:
    """The indexed columns of ``session``'s row (the rail filters and recency)."""

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
    )


def cache_capacity() -> int:
    """How many session rows stay resident (file -> env -> default)."""

    return max(
        1,
        conf.resolve(
            "sessions.cache_records",
            env="CLIO_SESSIONS_CACHE_RECORDS",
            default=1024,
            cast=conf.as_int,
        ),
    )


__all__ = [
    "InvalidCursorError",
    "PageQuery",
    "RecordColumns",
    "SessionRecords",
    "cache_capacity",
    "decode_cursor",
    "encode_cursor",
    "record_columns",
]
