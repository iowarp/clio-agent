"""The artifact registry served from clio-core records (``artifact_index`` kind).

Owner module (no accretion). clio-core is the memory: the registry's lookups are
POINT GETS on records keyed by what callers already know (workspace + name, relay
artifact id, session id, call id), behind a bounded in-process cache. Nothing here
lists the tag (a full-tag ``GetContainedBlobs``) and boot folds nothing — the
one-time migration (:mod:`~clio_agent.gact.artifacts.registry_migration`) writes
these records once from the full fold for a store that predates them.

The ``artifact.*`` event log stays the source of truth. A live write puts its index
record BEFORE the event is appended, marked ``pending`` under the event's id; the
semantic-event emitter confirms it once the event is recorded
(:func:`confirm_artifact_event`). A crash between the two leaves only a pending
hint: the first read of that record after :data:`PENDING_GRACE_S` rechecks the
emitting session's ``_events`` log and keeps (event found) or drops (no event) the
entry, each with a typed reason; an unreadable log keeps it, typed.

Records (``<prefix>/<sha256 of the key>``)::

    m/marker                  migration marker (present => no boot fold)
    m/workspaces              workspace ids that hold records (small)
    w/<ws>                    names in one workspace, creation order
    r/<ws,name>               {record, alias_keys, pending}
    a/<artifact_id>           [workspace_id, name]
    p/<session_id>            artifact ids whose producer is that session
    t/<call_id>               {transform, pending}
    s/<session_id>            call ids recorded in that session
    g/<artifact_id>           call ids that generated the artifact
    u/<artifact_id>           call ids that used the artifact
    x/<session_id>            {ids, pending} — the same-sha dedup USE set
    e/<artifact_id>           {annotation, pending} — dedup enrichment

Cross-process coherence: the cache is write-through for THIS process only, and the
store has no compare-and-set, so two processes minting the same ``(workspace,
name)`` at once can race on the version number (reported in the design doc).
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from collections import OrderedDict
from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING, Any, Optional

from clio_agent.arc.batch_put import PutRecord
from clio_agent.errors import ClioError
from clio_agent.gact.artifacts.records import ArtifactRecord, ArtifactVersion
from clio_agent.gact.artifacts.registry_backend import AliasKey, BoundedSet, PendingWrite

if TYPE_CHECKING:
    from fastapi import FastAPI

    from clio_agent.arc.store_protocol import ARCStore

logger = logging.getLogger(__name__)

#: The dedicated ARC kind (``clio_agent.arc.storage.ARC_KINDS``).
ARTIFACT_INDEX_KIND = "artifact_index"
MARKER_KEY = "m/marker"
WORKSPACES_KEY = "m/workspaces"
#: A pending entry younger than this may still be in flight in another process.
PENDING_GRACE_S = 120.0
_CACHE_ENTRIES = 4096
_SEEN_EVENT_IDS = 65536
#: What a clio-core record read/write can raise (no blind except).
STORE_ERRORS: tuple[type[BaseException], ...] = (ClioError, RuntimeError, OSError, ValueError)

#: ``(session_id, event_id) -> True`` (event found) / ``False`` (absent) / ``None`` (unreadable).
EventCheck = Callable[[str, str], Optional[bool]]


def index_key(prefix: str, *parts: str) -> str:
    """The record name for a key: a fixed prefix + a digest of its parts."""
    digest = hashlib.sha256(json.dumps(list(parts)).encode("utf-8")).hexdigest()[:40]
    return f"{prefix}/{digest}"


def encode(value: Any) -> bytes:
    """JSON-encode a record body."""
    return json.dumps(value, separators=(",", ":"), default=str).encode("utf-8")


def record_body(
    record: ArtifactRecord, alias_keys: dict[str, AliasKey], pending: dict[str, Any]
) -> dict[str, Any]:
    """The persisted body of one ``r/`` record."""
    return {
        "record": record.model_dump(mode="json"),
        "alias_keys": {alias: list(key) for alias, key in alias_keys.items()},
        "pending": pending,
    }


class StoreBackend:
    """Registry backend over clio-core ``artifact_index`` records + a bounded cache.

    Cached values are the decoded bodies (``dict`` / ``list``) keyed by record name;
    a record's ``ArtifactRecord`` object is cached inside its body so callers keep
    the same live object across calls (an alias move is visible on the record a
    route already holds), exactly as with the in-memory projection.
    """

    def __init__(
        self,
        store: "ARCStore",
        *,
        event_check: EventCheck,
        on_write_failure: Callable[[], None] = lambda: None,
        cache_entries: int = _CACHE_ENTRIES,
        grace_s: float = PENDING_GRACE_S,
    ) -> None:
        self._store = store
        self._event_check = event_check
        self._on_write_failure = on_write_failure
        self._cache: OrderedDict[str, Any] = OrderedDict()
        self._cache_entries = cache_entries
        self._grace_s = grace_s
        self._inflight: dict[str, list[str]] = {}
        self.seen_event_ids: Any = BoundedSet(_SEEN_EVENT_IDS)
        #: The last typed read/write degrade (for diagnostics/tests); ``None`` when clean.
        self.last_degrade: Optional[dict[str, Any]] = None

    # ---- raw record I/O ---------------------------------------------------

    def _remember(self, key: str, value: Any) -> None:
        self._cache[key] = value
        self._cache.move_to_end(key)
        while len(self._cache) > self._cache_entries:
            self._cache.popitem(last=False)

    def _degrade(self, reason: str, key: str, exc: BaseException) -> None:
        self.last_degrade = {"reason": reason, "key": key, "error": repr(exc)}
        logger.warning("artifact index degraded reason=%s key=%s cause=%r", reason, key, exc)

    def _read(self, key: str) -> Any:
        try:
            raw = self._store.get(ARTIFACT_INDEX_KIND, key)
            return None if raw is None else json.loads(raw)
        except STORE_ERRORS as exc:
            self._degrade("artifact_index_read_failed", key, exc)
            return None

    def _write(self, key: str, value: Any) -> None:
        self._remember(key, value)
        try:
            self._store.put(ARTIFACT_INDEX_KIND, key, encode(_persistable(value)))
        except STORE_ERRORS as exc:
            self._degrade("artifact_index_write_failed", key, exc)
            self._on_write_failure()

    def _delete(self, key: str) -> None:
        self._cache.pop(key, None)
        try:
            self._store.delete(ARTIFACT_INDEX_KIND, key)
        except STORE_ERRORS as exc:
            self._degrade("artifact_index_write_failed", key, exc)
            self._on_write_failure()

    def _list(self, key: str) -> list[str]:
        cached = self._cache.get(key)
        if cached is None:
            loaded = self._read(key)
            cached = [str(item) for item in loaded] if isinstance(loaded, list) else []
            if loaded is not None:
                self._remember(key, cached)
        else:
            self._cache.move_to_end(key)
        return cached

    def _append(self, key: str, item: str) -> None:
        items = self._list(key)
        if item not in items:
            self._write(key, [*items, item])

    def _body(self, key: str, decode: Callable[[dict[str, Any]], dict[str, Any]]) -> Any:
        """A cached-or-loaded record body (rechecking any stale pending entries)."""
        cached = self._cache.get(key)
        if cached is not None:
            self._cache.move_to_end(key)
            return cached
        loaded = self._read(key)
        if not isinstance(loaded, dict):
            return None
        try:
            body = decode(loaded)
        except (ValueError, TypeError, KeyError) as exc:
            self._degrade("artifact_index_record_malformed", key, exc)
            return None
        self._remember(key, body)
        return self._recheck(key, body)

    # ---- pending: confirm + recheck ----------------------------------------

    def _mark_pending(
        self, key: str, body: dict[str, Any], pending: Optional[PendingWrite], undo: dict[str, Any]
    ) -> None:
        if pending is None or not pending.event_id:
            return
        body["pending"][pending.event_id] = {"sid": pending.session_id, "at": time.time(), **undo}
        self._inflight.setdefault(pending.event_id, []).append(key)

    def confirm(self, event_id: str) -> None:
        """The event for ``event_id`` is recorded: clear its pending marks."""
        for key in self._inflight.pop(event_id, ()):
            body = self._cache.get(key)
            if body is None:
                loaded = self._read(key)
                body = _decode_any(key, loaded) if isinstance(loaded, dict) else None
            if body is not None and body.get("pending", {}).pop(event_id, None) is not None:
                self._write(key, body)

    def _recheck(self, key: str, body: dict[str, Any]) -> Any:
        """Resolve pending entries older than the grace against the event log."""
        pending = body.get("pending") or {}
        changed = False
        for event_id, info in list(pending.items()):
            if (
                event_id in self._inflight
                or time.time() - float(info.get("at") or 0) < self._grace_s
            ):
                continue
            found = self._event_check(str(info.get("sid") or ""), event_id)
            if found is None:
                logger.warning(
                    "artifact index pending entry kept reason=artifact_index_recheck_unreadable "
                    "key=%s event_id=%s session=%s",
                    key,
                    event_id,
                    info.get("sid"),
                )
                continue
            del pending[event_id]
            changed = True
            if not found:
                logger.warning(
                    "artifact index entry dropped reason=artifact_index_orphan_dropped key=%s "
                    "event_id=%s op=%s (written before a crash; its event was never appended)",
                    key,
                    event_id,
                    info.get("op"),
                )
                if _undo(body, info) == "delete":
                    self._delete(key)
                    return None
        if changed:
            self._write(key, body)
        return body

    # ---- records -----------------------------------------------------------

    def _entry(self, workspace_id: str, name: str) -> Optional[dict[str, Any]]:
        return self._body(index_key("r", workspace_id, name), _decode_record)

    def record(self, workspace_id: str, name: str) -> Optional[ArtifactRecord]:
        """The record for ``(workspace_id, name)``, or ``None``."""
        entry = self._entry(workspace_id, name)
        return None if entry is None else entry["record"]

    def ensure_record(self, workspace_id: str, name: str) -> ArtifactRecord:
        """The record for ``(workspace_id, name)``, created (unsaved) when absent."""
        entry = self._entry(workspace_id, name)
        if entry is None:
            entry = {
                "record": ArtifactRecord(workspace_id=workspace_id, name=name),
                "alias_keys": {},
                "pending": {},
                "new": True,
            }
            self._remember(index_key("r", workspace_id, name), entry)
        return entry["record"]

    def _entry_for(self, record: ArtifactRecord) -> dict[str, Any]:
        """The cached body holding ``record`` (reloaded if evicted; the object wins)."""
        key = index_key("r", record.workspace_id, record.name)
        entry = self._cache.get(key) or self._body(key, _decode_record)
        if entry is None:
            entry = {"alias_keys": {}, "pending": {}, "new": True}
        if entry.get("record") is not record:
            entry["record"] = record
            self._remember(key, entry)
        return entry

    def save_record(
        self,
        record: ArtifactRecord,
        *,
        new_version: Optional[ArtifactVersion] = None,
        pending: Optional[PendingWrite] = None,
        undo: Optional[dict[str, Any]] = None,
    ) -> None:
        """Persist ``record`` — secondary indexes first, then the record itself."""
        ws, name = record.workspace_id, record.name
        entry = self._entry_for(record)
        if entry.pop("new", False):
            self._append(WORKSPACES_KEY, ws)
            self._append(index_key("w", ws), name)
        if new_version is not None:
            self._write(index_key("a", new_version.artifact_id), [ws, name])
            producer_sid = str(new_version.producer.get("session_id") or "")
            if producer_sid:
                self._append(index_key("p", producer_sid), new_version.artifact_id)
            undo = {"op": "version", "version": new_version.version}
        self._mark_pending(index_key("r", ws, name), entry, pending, undo or {})
        self._write(index_key("r", ws, name), entry)

    def workspace_records(self, workspace_id: str) -> list[ArtifactRecord]:
        """Every record in ``workspace_id``, in creation order."""
        records = (
            self.record(workspace_id, name) for name in self._list(index_key("w", workspace_id))
        )
        return [record for record in records if record is not None]

    def all_records(self) -> list[ArtifactRecord]:
        """Every record of every workspace (introspection; walks the small workspace list)."""
        return [r for ws in self._list(WORKSPACES_KEY) for r in self.workspace_records(ws)]

    def locate(self, artifact_id: str) -> Optional[tuple[ArtifactRecord, ArtifactVersion]]:
        """The ``(record, version)`` carrying ``artifact_id``, or ``None``."""
        where = self._list(index_key("a", artifact_id))
        if len(where) != 2:
            return None
        record = self.record(where[0], where[1])
        if record is None:
            return None
        version = next((v for v in record.versions if v.artifact_id == artifact_id), None)
        return None if version is None else (record, version)

    def produced_in(
        self, session_ids: Iterable[str]
    ) -> list[tuple[ArtifactRecord, ArtifactVersion]]:
        """Versions whose ``producer.session_id`` is one of ``session_ids``."""
        found: list[tuple[ArtifactRecord, ArtifactVersion]] = []
        for sid in session_ids:
            for artifact_id in self._list(index_key("p", sid)):
                hit = self.locate(artifact_id)
                if hit is not None and str(hit[1].producer.get("session_id") or "") == sid:
                    found.append(hit)
        return found

    def alias_key(self, record: ArtifactRecord, alias: str) -> Optional[AliasKey]:
        """The recorded winning ``(at, event_id)`` for ``alias`` on ``record``."""
        return self._entry_for(record)["alias_keys"].get(alias)

    def set_alias_key(self, record: ArtifactRecord, alias: str, key: AliasKey) -> None:
        """Record ``key`` as the winning move (persisted by :meth:`save_record`)."""
        self._entry_for(record)["alias_keys"][alias] = key

    # ---- transforms --------------------------------------------------------

    def transform(self, call_id: str) -> Optional[Any]:
        """The TransformRecord for ``call_id``, or ``None``."""
        body = self._body(index_key("t", call_id), _decode_transform)
        return None if body is None else body["transform"]

    def add_transform(self, record: Any, *, pending: Optional[PendingWrite] = None) -> None:
        """Index a new TransformRecord: session/edge lists first, then the record."""
        self._append(index_key("s", record.session_id), record.call_id)
        for edge in record.generated:
            if edge.artifact_id:
                self._append(index_key("g", edge.artifact_id), record.call_id)
        for edge in record.used:
            if edge.artifact_id:
                self._append(index_key("u", edge.artifact_id), record.call_id)
        key = index_key("t", record.call_id)
        body: dict[str, Any] = {"transform": record, "pending": {}}
        self._mark_pending(key, body, pending, {"op": "transform"})
        self._write(key, body)

    def _transforms(self, call_ids: list[str]) -> list[Any]:
        found = (self.transform(call_id) for call_id in call_ids)
        return [record for record in found if record is not None]

    def session_transforms(self, session_id: str) -> list[Any]:
        """TransformRecords produced in ``session_id``, in record order."""
        found = self._transforms(self._list(index_key("s", session_id)))
        return [t for t in found if t.session_id == session_id]

    def generating(self, artifact_id: str) -> list[Any]:
        """TransformRecords with a generated edge on ``artifact_id``, in record order."""
        return self._transforms(self._list(index_key("g", artifact_id)))

    def using(self, artifact_id: str) -> list[Any]:
        """TransformRecords with a used edge on ``artifact_id``, in record order."""
        return self._transforms(self._list(index_key("u", artifact_id)))

    def all_transforms(self) -> list[Any]:
        """Not served: no record lists every transform (query by session or artifact)."""
        raise NotImplementedError(
            "artifact_index_unsupported_query: all_transforms is not served from clio-core "
            "records; use transforms_for_session / transforms_generating / transforms_using"
        )

    # ---- use + enrichment --------------------------------------------------

    def used(self, session_id: str) -> set[str]:
        """Relay ids ``session_id`` used via a same-sha dedup."""
        body = self._body(index_key("x", session_id), _decode_plain)
        return set() if body is None else set(body.get("ids") or ())

    def add_used(
        self, session_id: str, artifact_id: str, *, pending: Optional[PendingWrite] = None
    ) -> None:
        """Add ``artifact_id`` to ``session_id``'s USE set."""
        key = index_key("x", session_id)
        body = self._body(key, _decode_plain) or {"ids": [], "pending": {}}
        body["ids"] = [*body.get("ids", []), artifact_id]
        self._mark_pending(key, body, pending, {"op": "used", "artifact_id": artifact_id})
        self._write(key, body)

    def enrichment(self, artifact_id: str) -> str:
        """The supplemental annotation attached to ``artifact_id``, or ``""``."""
        body = self._body(index_key("e", artifact_id), _decode_plain)
        return "" if body is None else str(body.get("annotation") or "")

    def set_enrichment(
        self, artifact_id: str, annotation: str, *, pending: Optional[PendingWrite] = None
    ) -> None:
        """Attach ``annotation`` to ``artifact_id``."""
        key = index_key("e", artifact_id)
        body: dict[str, Any] = {"annotation": annotation, "pending": {}}
        self._mark_pending(key, body, pending, {"op": "enrich"})
        self._write(key, body)


# ---- body codecs ------------------------------------------------------------


def _decode_record(raw: dict[str, Any]) -> dict[str, Any]:
    return {
        "record": ArtifactRecord.model_validate(raw["record"]),
        "alias_keys": {
            str(alias): (str(key[0]), str(key[1]))
            for alias, key in (raw.get("alias_keys") or {}).items()
        },
        "pending": dict(raw.get("pending") or {}),
    }


def _decode_transform(raw: dict[str, Any]) -> dict[str, Any]:
    from clio_schemas import TransformRecord  # noqa: PLC0415

    return {
        "transform": TransformRecord.model_validate(raw["transform"]),
        "pending": dict(raw.get("pending") or {}),
    }


def _decode_plain(raw: dict[str, Any]) -> dict[str, Any]:
    return {**raw, "pending": dict(raw.get("pending") or {})}


def _decode_any(key: str, raw: dict[str, Any]) -> dict[str, Any]:
    prefix = key.split("/", 1)[0]
    if prefix == "r":
        return _decode_record(raw)
    if prefix == "t":
        return _decode_transform(raw)
    return _decode_plain(raw)


def _persistable(value: Any) -> Any:
    """The JSON-ready form of a cached body (models dumped, cache-only flags dropped)."""
    if isinstance(value, list):
        return value
    if "record" in value:
        return record_body(value["record"], value["alias_keys"], value["pending"])
    if "transform" in value:
        return {
            "transform": value["transform"].model_dump(mode="json"),
            "pending": value["pending"],
        }
    return value


def _undo(body: dict[str, Any], info: dict[str, Any]) -> str:
    """Roll back one orphaned live write in ``body``; ``"delete"`` drops the record."""
    op = info.get("op")
    if op in ("transform", "enrich"):
        return "delete"
    if op == "used":
        body["ids"] = [i for i in body.get("ids", []) if i != info.get("artifact_id")]
        return ""
    record: ArtifactRecord = body["record"]
    if op == "version":
        record.versions = [v for v in record.versions if v.version != info.get("version")]
        if record.versions:
            record.aliases["latest"] = record.versions[-1].version
        else:
            record.aliases.pop("latest", None)
            if not record.aliases:
                return "delete"
    elif op == "alias":
        alias = str(info.get("alias") or "")
        prev, prev_key = info.get("prev"), info.get("prev_key")
        if prev is None:
            record.aliases.pop(alias, None)
        else:
            record.aliases[alias] = int(prev)
        if prev_key:
            body["alias_keys"][alias] = (str(prev_key[0]), str(prev_key[1]))
        else:
            body["alias_keys"].pop(alias, None)
    return ""


# ---- wiring -----------------------------------------------------------------


def _arc(app: "FastAPI") -> Any:
    from clio_agent.gact.runtime.globals import _PROCESS_ARC  # noqa: PLC0415

    return getattr(app.state, "arc", None) or _PROCESS_ARC


def index_store(app: "FastAPI") -> Optional["ARCStore"]:
    """The clio-core record store the index lives in, or ``None`` (typed, logged)."""
    store = getattr(_arc(app), "_store", None)
    if all(callable(getattr(store, op, None)) for op in ("get", "put", "put_many", "delete")):
        return store
    logger.warning(
        "artifact index unavailable reason=artifact_index_unavailable detail=no clio-core "
        "record store on the ARC; the registry is the in-process full fold"
    )
    return None


def session_event_check(app: "FastAPI") -> EventCheck:
    """Recheck one pending entry: is ``event_id`` in ``session_id``'s ``_events`` log?"""

    def check(session_id: str, event_id: str) -> Optional[bool]:
        arc = _arc(app)
        observer = getattr(arc, "_live", None) or getattr(arc, "live", None)
        read = getattr(observer, "iter_session_event_segments", None)
        if not session_id or not callable(read):
            return None
        try:
            for segment in read(session_id):
                content = getattr(segment, "content", None)
                payload = content.get("payload") if isinstance(content, dict) else None
                if isinstance(payload, dict) and payload.get("event_id") == event_id:
                    return True
        except STORE_ERRORS as exc:
            logger.warning(
                "artifact index recheck unreadable reason=artifact_index_recheck_unreadable "
                "session=%s cause=%r",
                session_id,
                exc,
            )
            return None
        return False

    return check


def invalidate_marker(store: "ARCStore") -> None:
    """A lost index write: drop the marker so the next boot re-migrates from the log."""
    try:
        store.delete(ARTIFACT_INDEX_KIND, MARKER_KEY)
        logger.warning(
            "artifact index marker dropped reason=artifact_index_write_failed; the next boot "
            "rebuilds the index from the event log once"
        )
    except STORE_ERRORS as exc:
        logger.error(
            "artifact index marker could not be dropped reason=artifact_index_repair_unrecorded "
            "cause=%r; records written since are missing until the index is rebuilt",
            exc,
        )


def store_backend(app: "FastAPI", store: "ARCStore") -> StoreBackend:
    """The store-backed registry backend for ``app``."""
    return StoreBackend(
        store,
        event_check=session_event_check(app),
        on_write_failure=lambda: invalidate_marker(store),
    )


def put_bodies(store: "ARCStore", bodies: dict[str, Any], *, chunk: int = 256) -> None:
    """Write many record bodies (migration) in bounded batches."""
    items = [PutRecord(name=key, data=encode(body)) for key, body in bodies.items()]
    for start in range(0, len(items), chunk):
        store.put_many(ARTIFACT_INDEX_KIND, items[start : start + chunk])


def confirm_artifact_event(app: Any, payload: Optional[dict[str, Any]]) -> None:
    """Emitter hook: the ``artifact.*`` event carrying ``payload`` is now recorded."""
    event_id = (payload or {}).get("event_id")
    registry = getattr(getattr(app, "state", None), "artifact_registry", None)
    confirm = getattr(registry, "confirm_event", None)
    if event_id and callable(confirm):
        confirm(str(event_id))


__all__ = [
    "ARTIFACT_INDEX_KIND",
    "MARKER_KEY",
    "PENDING_GRACE_S",
    "STORE_ERRORS",
    "StoreBackend",
    "confirm_artifact_event",
    "index_key",
    "index_store",
    "put_bodies",
    "record_body",
    "session_event_check",
    "store_backend",
]
