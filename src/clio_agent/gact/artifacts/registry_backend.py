"""Where the artifact registry's projection lives — the in-process backend.

Owner module (no accretion): :class:`~clio_agent.gact.artifacts.registry.ArtifactRegistry`
owns the fold / mint / alias DECISIONS; a backend owns where their results live.
Two backends share one interface:

* :class:`MemoryBackend` (here) — plain dicts. It is the projection a full fold
  builds (the one-time migration source, and the registry of a process with no
  clio-core record store), so its iteration order is the fold order.
* :class:`~clio_agent.gact.artifacts.registry_store.StoreBackend` — clio-core
  ``artifact_index`` records served by point gets behind a bounded cache.

Every method is called with the registry lock held. ``pending`` is the live write's
:class:`PendingWrite` (``None`` on a fold: a folded fact came from the log, so it
is already confirmed); the memory backend has no durable state to confirm.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Optional

from clio_agent.gact.artifacts.records import ArtifactRecord, ArtifactVersion

#: An alias move's last-writer-wins key: ``(at, event_id)``.
AliasKey = tuple[str, str]


@dataclass(frozen=True)
class PendingWrite:
    """A live index write whose ``artifact.*`` event has not been appended yet.

    ``event_id`` is the id the event payload will carry; ``session_id`` is the
    session whose ``_events`` log the event goes to (where a recheck looks).
    """

    event_id: str
    session_id: str


class BoundedSet:
    """A set that forgets its oldest members past ``cap`` (event-id dedup memory)."""

    def __init__(self, cap: int) -> None:
        self._cap = cap
        self._items: OrderedDict[str, None] = OrderedDict()

    def __contains__(self, item: object) -> bool:
        return item in self._items

    def add(self, item: str) -> None:
        """Remember ``item``, dropping the oldest member when over the cap."""
        self._items[item] = None
        self._items.move_to_end(item)
        while len(self._items) > self._cap:
            self._items.popitem(last=False)


class MemoryBackend:
    """The whole projection in process memory (fold target; no clio-core store)."""

    def __init__(self) -> None:
        self.seen_event_ids: Any = set()
        self._records: dict[tuple[str, str], ArtifactRecord] = {}
        self._alias_keys: dict[tuple[str, str, str], AliasKey] = {}
        #: TransformRecords keyed by ``call_id``, in record order.
        self._transforms: dict[str, Any] = {}
        self._used: dict[str, set[str]] = {}
        self._enrichment: dict[str, str] = {}

    # ---- records -----------------------------------------------------------

    def record(self, workspace_id: str, name: str) -> Optional[ArtifactRecord]:
        """The record for ``(workspace_id, name)``, or ``None``."""
        return self._records.get((workspace_id, name))

    def ensure_record(self, workspace_id: str, name: str) -> ArtifactRecord:
        """The record for ``(workspace_id, name)``, created empty when absent."""
        record = self._records.get((workspace_id, name))
        if record is None:
            record = ArtifactRecord(workspace_id=workspace_id, name=name)
            self._records[(workspace_id, name)] = record
        return record

    def save_record(
        self,
        record: ArtifactRecord,
        *,
        new_version: Optional[ArtifactVersion] = None,
        pending: Optional[PendingWrite] = None,
        undo: Optional[dict[str, Any]] = None,
    ) -> None:
        """Records are mutated in place; nothing further to persist."""

    def workspace_records(self, workspace_id: str) -> list[ArtifactRecord]:
        """Every record in ``workspace_id``, in creation order."""
        return [r for (ws, _n), r in self._records.items() if ws == workspace_id]

    def all_records(self) -> list[ArtifactRecord]:
        """Every record, in creation order."""
        return list(self._records.values())

    def locate(self, artifact_id: str) -> Optional[tuple[ArtifactRecord, ArtifactVersion]]:
        """The ``(record, version)`` carrying ``artifact_id``, or ``None``."""
        for record in self._records.values():
            for version in record.versions:
                if version.artifact_id == artifact_id:
                    return (record, version)
        return None

    def produced_in(
        self, session_ids: Iterable[str]
    ) -> list[tuple[ArtifactRecord, ArtifactVersion]]:
        """Versions whose ``producer.session_id`` is one of ``session_ids``."""
        wanted = set(session_ids)
        return [
            (record, version)
            for record in self._records.values()
            for version in record.versions
            if str(version.producer.get("session_id") or "") in wanted
        ]

    def alias_key(self, record: ArtifactRecord, alias: str) -> Optional[AliasKey]:
        """The recorded winning ``(at, event_id)`` for ``alias`` on ``record``."""
        return self._alias_keys.get((record.workspace_id, record.name, alias))

    def all_alias_keys(self) -> dict[tuple[str, str], dict[str, AliasKey]]:
        """Every recorded alias key, grouped by record (for the migration export)."""
        grouped: dict[tuple[str, str], dict[str, AliasKey]] = {}
        for (ws, name, alias), key in self._alias_keys.items():
            grouped.setdefault((ws, name), {})[alias] = key
        return grouped

    def set_alias_key(self, record: ArtifactRecord, alias: str, key: AliasKey) -> None:
        """Record ``key`` as the winning move for ``alias`` (persisted by ``save_record``)."""
        self._alias_keys[(record.workspace_id, record.name, alias)] = key

    # ---- transforms --------------------------------------------------------

    def transform(self, call_id: str) -> Optional[Any]:
        """The TransformRecord for ``call_id``, or ``None``."""
        return self._transforms.get(call_id)

    def add_transform(self, record: Any, *, pending: Optional[PendingWrite] = None) -> None:
        """Store a new TransformRecord (the caller checked ``call_id`` is new)."""
        self._transforms[record.call_id] = record

    def session_transforms(self, session_id: str) -> list[Any]:
        """TransformRecords produced in ``session_id``, in record order."""
        return [t for t in self._transforms.values() if t.session_id == session_id]

    def generating(self, artifact_id: str) -> list[Any]:
        """TransformRecords with a generated edge on ``artifact_id``, in record order."""
        return [
            t
            for t in self._transforms.values()
            if any(e.artifact_id == artifact_id for e in t.generated)
        ]

    def using(self, artifact_id: str) -> list[Any]:
        """TransformRecords with a used edge on ``artifact_id``, in record order."""
        return [
            t
            for t in self._transforms.values()
            if any(e.artifact_id == artifact_id for e in t.used)
        ]

    def all_transforms(self) -> list[Any]:
        """Every TransformRecord, in record order."""
        return list(self._transforms.values())

    # ---- use + enrichment indexes -----------------------------------------

    def used(self, session_id: str) -> set[str]:
        """Relay ids ``session_id`` used via a same-sha dedup."""
        return set(self._used.get(session_id, ()))

    def all_used(self) -> dict[str, list[str]]:
        """The whole USE index (for the migration export)."""
        return {sid: sorted(ids) for sid, ids in self._used.items()}

    def add_used(
        self, session_id: str, artifact_id: str, *, pending: Optional[PendingWrite] = None
    ) -> None:
        """Add ``artifact_id`` to ``session_id``'s USE set."""
        self._used.setdefault(session_id, set()).add(artifact_id)

    def enrichment(self, artifact_id: str) -> str:
        """The supplemental annotation attached to ``artifact_id``, or ``""``."""
        return self._enrichment.get(artifact_id, "")

    def all_enrichment(self) -> dict[str, str]:
        """The whole enrichment index (for the migration export)."""
        return dict(self._enrichment)

    def set_enrichment(
        self, artifact_id: str, annotation: str, *, pending: Optional[PendingWrite] = None
    ) -> None:
        """Attach ``annotation`` to ``artifact_id``."""
        self._enrichment[artifact_id] = annotation

    # ---- pending confirmation ---------------------------------------------

    def confirm(self, event_id: str) -> None:
        """Nothing durable to confirm in memory."""


__all__ = ["AliasKey", "BoundedSet", "MemoryBackend", "PendingWrite"]
