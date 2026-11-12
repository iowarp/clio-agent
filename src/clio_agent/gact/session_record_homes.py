"""One CLIO home's session records on another home's clio-core daemon.

The records live under the home's name prefix (:mod:`clio_agent.arc.home_namespace`);
bare rows it wrote before the prefix existed are adopted into it and marked claimed,
and the daemon's own home stops listing rows another home claimed.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Any, Optional

from clio_agent.arc.batch_put import PutRecord
from clio_agent.gact.session_index_columns import decode_bucket, extension_name
from clio_agent.gact.session_records import (
    _BUCKET,
    _ROW,
    _WRITE_WAIT_SECONDS,
    INDEX_BUCKETS,
    KIND,
    WRITE_FAILED_REASON,
    bucket_of,
)
from clio_agent.runtime.stream_audit import stream_audit

if TYPE_CHECKING:
    from clio_agent.gact.session_index_columns import RecordColumns
    from clio_agent.gact.session_records import SessionRecords

logger = logging.getLogger("clio_agent.gact.session_records")

#: Bare marker of a pre-namespace row another CLIO home adopted (value: its namespace).
CLAIMED = "claimed/"
LEGACY_ADOPTED_REASON = "session_records_home_adopted"


def home_owns_legacy(app: Any) -> Callable[[Any], bool]:
    """Recognise this home's sessions among bare, pre-namespace session records.

    A session is this home's when its workspace is in this home's registry (workspace
    ids are random per home, except the shared ``ws_default``) or this home holds its
    transcript on file. Read once, at the first row asked about.
    """
    from clio_agent.gact.workspaces import DEFAULT_WORKSPACE_ID  # noqa: PLC0415

    known: list[set[str]] = []

    def owns(row: Any) -> bool:
        if not known:
            workspaces = getattr(app.state, "workspaces", None)
            ids = {ws.id for ws in workspaces.list()} if workspaces is not None else set()
            ids.discard(DEFAULT_WORKSPACE_ID)
            store = getattr(app.state, "message_store", None)
            known.extend((ids, set(store.session_ids()) if store is not None else set()))
        return row.workspace_id in known[0] or row.id in known[1]

    return owns


class PrefixedStore:
    """One CLIO home's session records inside a shared store: every name under ``prefix``
    (:mod:`clio_agent.arc.home_namespace`)."""

    def __init__(self, store: Any, prefix: str) -> None:
        self.store = store
        self.prefix = prefix

    def put_many(self, kind: str, records: Sequence[PutRecord]) -> None:
        self.store.put_many(
            kind, [PutRecord(self.prefix + record.name, record.data) for record in records]
        )

    def get(self, kind: str, name: str) -> Optional[bytes]:
        return self.store.get(kind, self.prefix + name)

    def delete(self, kind: str, name: str) -> None:
        self.store.delete(kind, self.prefix + name)


def claimed(store: Any) -> dict[str, str]:
    """``{session id: claiming namespace}``: bare rows another home adopted."""
    scan = getattr(store, "scan_names", None)
    if scan is None:
        return {}
    claims: dict[str, str] = {}
    for name in scan(KIND, CLAIMED):
        body = store.get(KIND, name)
        claims[name[len(CLAIMED) :]] = body.decode() if body else ""
    return claims


def adopt_legacy(
    records: SessionRecords,
    store: Any,
    namespace: str,
    owns: Optional[Callable[[RecordColumns], bool]],
) -> None:
    """Copy this home's pre-namespace bare rows under its prefix and mark them claimed.

    A row is this home's when ``owns`` says so or its parent session is this home's.
    Rows another home claimed, or no one recognises, are left as they are: an
    upgrade may leave a session with the daemon's owner, but never hides it.
    """
    if owns is None:
        return
    claims = claimed(store)
    legacy: dict[str, RecordColumns] = {}
    stale: set[str] = set()
    for number in range(INDEX_BUCKETS):
        bucket = f"{_BUCKET}{number:02d}"
        rows, unfilled = decode_bucket(
            store.get(KIND, bucket), store.get(KIND, extension_name(bucket))
        )
        stale |= unfilled
        for row in rows:
            # A claim of our own whose copy never landed is adopted again.
            if row.id not in records._columns and claims.get(row.id, namespace) == namespace:
                legacy[row.id] = row
    mine = {sid for sid, row in legacy.items() if owns(row)}
    grew = True
    while grew:
        grew = False
        for sid, row in legacy.items():
            parent = row.parent_session_id
            if sid not in mine and parent and (parent in mine or parent in records._columns):
                mine.add(sid)
                grew = True
    changes: dict[str, Optional[bytes]] = {}
    for sid in sorted(mine):
        body = store.get(KIND, _ROW + sid)
        if body is None:
            continue
        records._index(legacy[sid])
        if sid in stale:
            records._unfilled.add(sid)
        changes[_ROW + sid] = body
    if not changes:
        return
    adopted = [name[len(_ROW) :] for name in changes]
    for bucket in {bucket_of(sid) for sid in adopted}:
        changes.update(records._bucket_bodies(bucket))
    ticket = records._writer.submit(changes)
    if not records._writer.wait(ticket, _WRITE_WAIT_SECONDS):
        # The copies are not confirmed: leave the bare rows unclaimed (still listed
        # by the daemon's owner) and adopt again at the next attach.
        logger.warning(
            "legacy session adoption not confirmed reason=%s sessions=%d; retried next attach",
            WRITE_FAILED_REASON,
            len(adopted),
        )
        return
    store.put_many(KIND, [PutRecord(CLAIMED + sid, namespace.encode()) for sid in adopted])
    stream_audit(LEGACY_ADOPTED_REASON, reason=LEGACY_ADOPTED_REASON, sessions=len(adopted))
    logger.info(
        "adopted this home's pre-namespace sessions reason=%s namespace=%s sessions=%d",
        LEGACY_ADOPTED_REASON,
        namespace,
        len(adopted),
    )


def drop_claimed(records: SessionRecords, store: Any) -> None:
    """Stop listing bare rows another home adopted (their copies are that home's).

    Rows this home claimed itself, while it ran on another home's daemon, stay
    listed: the bare copies are still its own.
    """
    from clio_agent.arc import home_namespace  # noqa: PLC0415

    own = home_namespace.namespace_for(home_namespace.home_identity())
    others = [sid for sid, by in claimed(store).items() if by != own and sid in records._columns]
    for sid in others:
        records._unindex(sid)
    if others:
        logger.info(
            "sessions adopted by another CLIO home not listed here reason=%s sessions=%d",
            LEGACY_ADOPTED_REASON,
            len(others),
        )
