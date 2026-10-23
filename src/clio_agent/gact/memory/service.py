"""The one writer of long-term memory (owner module).

Every memory write -- a user's request through a tool, an accepted proposal, an edit on
the memory page, an import, a restore -- goes through :class:`MemoryService`, in the
GACT server, including writes asked for by delegated agents. A write:

1. holds the document's lock (one process writes memory: the GACT server);
2. checks the caller's ``sha256`` precondition against the stored text, so a stale
   writer gets :class:`MemoryConflict` (``memory_conflict``) carrying the current hash;
3. writes the previous text to ``memory_versions`` first, with the change's author;
4. writes the document and its search companion in one ``put_many``.

A failure after step 3 leaves the old document current and is raised typed (the
store's ``BatchPutError``). Removing a document archives it: its text is the last
version and the record leaves ``memories``.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
from dataclasses import asdict, dataclass, replace
from typing import Any, Literal

from clio_agent.arc.batch_put import PutRecord
from clio_agent.arc.memory_document import (
    Fact,
    MemoryDocument,
    forgetting,
    now_stamp,
    parse,
    search_text,
    serialize,
)
from clio_agent.arc.store_protocol import ARCStore
from clio_agent.errors import ClioError
from clio_agent.gact.memory.scope import (
    RecordName,
    check_scope,
    parse_record_name,
    record_name,
    scopes_name_re,
)
from clio_agent.gact.memory.store import DOCUMENTS, VERSIONS

AuthorKind = Literal["tool_call", "proposal", "user_edit", "import", "restore"]
_WORD = re.compile(r"[a-z0-9]+")
_PROFILE_DESCRIPTIONS = {
    "profile": "Who the user is: stable facts for months",
    "preferences": "How the user wants the agent to behave",
}


class MemoryConflict(ClioError):
    """A write's precondition no longer holds: someone changed the document first."""

    def __init__(self, name: str, *, expected: str | None, current: str | None) -> None:
        self.current_sha = current
        super().__init__(
            f"memory document {name} changed since it was read; re-read it and retry "
            f"on hash {current or '<absent>'}",
            error_type="memory_conflict",
            details={"name": name, "expected_sha": expected, "current_sha": current},
        )


class MemoryNotFound(ClioError):
    """No such memory document (or version)."""

    def __init__(self, name: str) -> None:
        super().__init__(
            f"no memory document {name}",
            error_type="memory_not_found",
            details={"name": name},
        )


@dataclass(frozen=True)
class Author:
    """Who made a change: the tool call, proposal, page edit, import or restore."""

    kind: AuthorKind
    id: str
    session: str = ""
    reason: str = ""


@dataclass(frozen=True)
class StoredDocument:
    """A document as stored, with the hash a later write must name."""

    name: RecordName
    document: MemoryDocument
    text: str
    sha: str


@dataclass(frozen=True)
class SearchHit:
    """A document BM25 ranked for a query, with its facts that share query words."""

    name: str
    category: str
    description: str
    score: float
    facts: tuple[Fact, ...]


@dataclass(frozen=True)
class Version:
    """The document before change ``n``, and who made change ``n``."""

    n: int
    text: str
    author: dict[str, str]
    at: str


def text_sha(text: str) -> str:
    """The precondition hash of a document's text."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _words(text: str) -> set[str]:
    return set(_WORD.findall(text.lower()))


class MemoryService:
    """Reads, searches and the single locked write path of long-term memory."""

    def __init__(self, store: ARCStore) -> None:
        self._store = store
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()

    @property
    def store(self) -> ARCStore:
        """The memory store (namespace ``memory``)."""
        return self._store

    def _lock(self, name: str) -> threading.Lock:
        with self._guard:
            return self._locks.setdefault(name, threading.Lock())

    # ---- reads ----

    def get(self, name: str | RecordName) -> StoredDocument | None:
        """The stored document at ``name``, or ``None``."""
        address = name if isinstance(name, RecordName) else parse_record_name(name)
        raw = self._store.get(DOCUMENTS, str(address))
        if raw is None:
            return None
        text = raw.decode("utf-8")
        return StoredDocument(address, parse(text), text, text_sha(text))

    def require(self, name: str | RecordName) -> StoredDocument:
        """The stored document at ``name``; raises :class:`MemoryNotFound` if absent."""
        stored = self.get(name)
        if stored is None:
            raise MemoryNotFound(str(name))
        return stored

    def documents(self, scopes: tuple[str, ...]) -> list[StoredDocument]:
        """Every document of ``scopes``, in scope order then name order."""
        out: list[StoredDocument] = []
        for scope in scopes:
            prefix = check_scope(scope) + "/"
            names = sorted(
                name
                for name in self._store.scan_names(DOCUMENTS, prefix)
                if name.count("/") == prefix.count("/") + 1
            )
            for name in names:
                stored = self.get(name)
                if stored is not None:
                    out.append(stored)
        return out

    def search(
        self, scopes: tuple[str, ...], query: str, *, category: str | None = None, limit: int = 8
    ) -> list[SearchHit]:
        """BM25 over the documents of ``scopes`` (optionally one category).

        Raises:
            SearchUnavailableError: clio-core cannot search (no indexer composed).
        """
        from clio_agent.arc.memory_segments import SearchUnavailableError  # noqa: PLC0415

        if not self._store.supports_search():
            reason = getattr(self._store, "search_degradation_reason", lambda: "")()
            raise SearchUnavailableError(reason or "clio_core_search_unavailable")
        ranked = self._store.search(
            DOCUMENTS, query, name_re=scopes_name_re(scopes, category), k=max(1, limit)
        )
        wanted = _words(query)
        hits: list[SearchHit] = []
        for name, score in ranked:
            stored = self.get(name)
            if stored is None:
                continue
            live = stored.document.live_facts
            matching = tuple(fact for fact in live if wanted & _words(fact.text)) or live
            hits.append(
                SearchHit(
                    name=name,
                    category=stored.name.category,
                    description=stored.document.description,
                    score=score,
                    facts=matching[:12],
                )
            )
        return hits

    def versions(self, name: str | RecordName) -> list[Version]:
        """The history of ``name``, oldest first."""
        prefix = f"{name}/"
        out: list[Version] = []
        for version_name in sorted(self._store.scan_names(VERSIONS, prefix)):
            raw = self._store.get(VERSIONS, version_name)
            if raw is None:
                continue
            record = json.loads(raw)
            out.append(
                Version(
                    n=int(version_name.rsplit("/", 1)[1]),
                    text=record["text"],
                    author=record["author"],
                    at=record["at"],
                )
            )
        return out

    def forgotten(self, scope: str) -> tuple[str, ...]:
        """Fact ids the user asked to forget in ``scope``."""
        stored = self.get(record_name(scope, "profile", "profile"))
        return stored.document.forgotten if stored else ()

    # ---- the write path ----

    def _next_version(self, name: str) -> int:
        taken = [int(v.rsplit("/", 1)[1]) for v in self._store.scan_names(VERSIONS, f"{name}/")]
        return max(taken, default=0) + 1

    def _record_version(self, name: str, previous: str, author: Author) -> None:
        n = self._next_version(name)
        record = {"text": previous, "author": asdict(author), "at": now_stamp()}
        self._store.put(VERSIONS, f"{name}/{n:06d}", json.dumps(record).encode("utf-8"))

    def write(
        self,
        name: str | RecordName,
        document: MemoryDocument,
        *,
        expected_sha: str | None,
        author: Author,
    ) -> StoredDocument:
        """Replace (or create) ``name`` with ``document``.

        Args:
            name: The document's address; its category and slug must match ``document``.
            document: The new content.
            expected_sha: The hash of the text the caller read, or ``None`` when the
                caller expects no document yet.
            author: Who made the change; recorded on the version.

        Raises:
            MemoryConflict: The stored document is not the one the caller read.
            BatchPutError: The document write failed after its version was recorded;
                the old document is still current.
        """
        address = name if isinstance(name, RecordName) else parse_record_name(name)
        if (document.category, document.name) != (address.category, address.slug):
            raise ValueError(f"document {document.category}/{document.name} is not {address}")
        text = serialize(document)
        key = str(address)
        with self._lock(key):
            current = self.get(address)
            current_sha = current.sha if current else None
            if expected_sha != current_sha:
                raise MemoryConflict(key, expected=expected_sha, current=current_sha)
            if current is not None and current.text == text:
                return current
            self._record_version(key, current.text if current else "", author)
            record = PutRecord(
                name=key, data=text.encode("utf-8"), search_text=search_text(document)
            )
            self._store.put_many(DOCUMENTS, [record])
        return StoredDocument(address, document, text, text_sha(text))

    def remove(self, name: str | RecordName, *, expected_sha: str, author: Author) -> None:
        """Archive ``name``: its text becomes the last version and it leaves ``memories``.

        Raises:
            MemoryConflict: The stored document is not the one the caller read.
        """
        key = str(name)
        with self._lock(key):
            current = self.get(name)
            current_sha = current.sha if current else None
            if current is None or expected_sha != current_sha:
                raise MemoryConflict(key, expected=expected_sha, current=current_sha)
            self._record_version(key, current.text, author)
            self._store.delete(DOCUMENTS, key)

    def restore(self, name: str | RecordName, n: int, *, author: Author) -> StoredDocument:
        """Make version ``n``'s text current again (itself a new version).

        Raises:
            MemoryNotFound: There is no version ``n``, or it is the empty state
                before the document was created.
        """
        version = next((v for v in self.versions(name) if v.n == n), None)
        if version is None or not version.text:
            raise MemoryNotFound(f"{name}@{n}")
        current = self.get(name)
        return self.write(
            name,
            parse(version.text),
            expected_sha=current.sha if current else None,
            author=author,
        )

    def ensure_singleton(self, scope: str, category: str) -> StoredDocument:
        """The scope's ``profile`` or ``preferences`` document, created empty if absent."""
        address = record_name(scope, category, category)
        stored = self.get(address)
        if stored is not None:
            return stored
        empty = MemoryDocument(
            name=category,
            description=_PROFILE_DESCRIPTIONS[category],
            category=category,  # type: ignore[arg-type]  # profile or preferences
            updated=now_stamp(),
        )
        try:
            return self.write(
                address, empty, expected_sha=None, author=Author("user_edit", "created")
            )
        except MemoryConflict:
            return self.require(address)  # created concurrently

    def forget(self, scope: str, fact_ids: tuple[str, ...], *, author: Author) -> None:
        """Add ``fact_ids`` to the forgotten list of ``scope``'s profile."""
        stored = self.ensure_singleton(scope, "profile")
        updated = forgetting(stored.document, fact_ids)
        if updated != stored.document:
            self.write(
                stored.name,
                replace(updated, updated=now_stamp()),
                expected_sha=stored.sha,
                author=author,
            )


def stored_to_wire(stored: StoredDocument) -> dict[str, Any]:
    """The JSON shape of a stored document for routes and tool results."""
    doc = stored.document
    return {
        "name": str(stored.name),
        "scope": stored.name.scope,
        "category": stored.name.category,
        "slug": stored.name.slug,
        "description": doc.description,
        "aliases": list(doc.aliases),
        "updated": doc.updated,
        "sha": stored.sha,
        "text": stored.text,
        "forgotten": list(doc.forgotten),
        "facts": [{**asdict(fact), "live": fact.live} for fact in doc.facts],
    }


__all__ = [
    "Author",
    "MemoryConflict",
    "MemoryNotFound",
    "MemoryService",
    "SearchHit",
    "StoredDocument",
    "Version",
    "stored_to_wire",
    "text_sha",
]
