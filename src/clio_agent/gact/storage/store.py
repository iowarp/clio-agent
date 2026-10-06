"""Transactional, namespaced metadata for connected sources and transfer operations."""

from __future__ import annotations

import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, TypeVar

from pydantic import BaseModel

from clio_agent.gact.storage.models import SourceRecord, TransferOperation, now

T = TypeVar("T", bound=BaseModel)


class SourceStore:
    """Small SQLite ledger; corrupted metadata fails explicitly instead of resetting ownership."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "sources.sqlite3"
        with self.transaction() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS records (kind TEXT, id TEXT, body TEXT NOT NULL, PRIMARY KEY(kind,id))"
            )
            db.execute("CREATE TABLE IF NOT EXISTS identity (id TEXT PRIMARY KEY)")
            identity = db.execute("SELECT id FROM identity").fetchone()
            self.clio_id = identity[0] if identity else "clio_" + uuid.uuid4().hex
            if identity is None:
                db.execute("INSERT INTO identity VALUES (?)", (self.clio_id,))

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Serialize metadata changes and always release the connection."""
        db = sqlite3.connect(self.path, timeout=30)
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def put(self, kind: str, identifier: str, value: BaseModel) -> None:
        """Atomically replace a typed record."""
        with self.transaction() as db:
            db.execute(
                "INSERT OR REPLACE INTO records VALUES (?, ?, ?)",
                (kind, identifier, value.model_dump_json()),
            )

    def get(self, kind: str, identifier: str, model: type[T]) -> T:
        """Load one typed record, refusing unknown identities."""
        with self.transaction() as db:
            row = db.execute(
                "SELECT body FROM records WHERE kind=? AND id=?", (kind, identifier)
            ).fetchone()
        if row is None:
            raise KeyError(identifier)
        return model.model_validate_json(row[0])

    def list(self, kind: str, model: type[T]) -> list[T]:
        """Return validated records in stable insertion order."""
        with self.transaction() as db:
            rows = db.execute(
                "SELECT body FROM records WHERE kind=? ORDER BY rowid", (kind,)
            ).fetchall()
        return [model.model_validate_json(row[0]) for row in rows]

    def update_operation(self, identifier: str, **changes: object) -> TransferOperation:
        """Update progress without losing a concurrent cancellation request."""
        with self.transaction() as db:
            row = db.execute(
                "SELECT body FROM records WHERE kind='operation' AND id=?", (identifier,)
            ).fetchone()
            if row is None:
                raise KeyError(identifier)
            operation = TransferOperation.model_validate_json(row[0])
            operation = TransferOperation.model_validate(
                {**operation.model_dump(), **changes, "updated_at": now()}
            )
            db.execute(
                "UPDATE records SET body=? WHERE kind='operation' AND id=?",
                (operation.model_dump_json(), identifier),
            )
        return operation

    def begin_operation(self, source_id: str, kind: str) -> TransferOperation:
        """Reject overlapping changes to the same source, including from another client."""
        with self.transaction() as db:
            rows = db.execute("SELECT body FROM records WHERE kind='operation'").fetchall()
            for row in rows:
                previous = TransferOperation.model_validate_json(row[0])
                if previous.source_id == source_id and previous.state in {"queued", "running"}:
                    raise ValueError("A transfer is already active for this source")
            operation = TransferOperation.model_validate(
                {"id": "transfer_" + uuid.uuid4().hex, "source_id": source_id, "kind": kind}
            )
            db.execute(
                "INSERT INTO records VALUES ('operation', ?, ?)",
                (operation.id, operation.model_dump_json()),
            )
        return operation

    def recover(self) -> None:
        """Mark interrupted local transfers; native jobs remain queryable by their job IDs."""
        for operation in self.list("operation", TransferOperation):
            if operation.state in {"queued", "running"}:
                self.update_operation(
                    operation.id, state="interrupted", error="CLIO restarted; resume the transfer."
                )
                source = self.get("source", operation.source_id, SourceRecord)
                source.source = source.source.model_copy(
                    update={"materialization": "stale" if source.manifest_id else "failed"}
                )
                self.put("source", source.source.id, source)
