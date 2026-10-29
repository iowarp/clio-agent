"""Derived task lookup and open-work records kept in clio-core.

clio-core is the task memory. The agent keeps no global index: it loads one task
tree on demand from the per-session event logs and finds that tree through two
small record kinds that live next to the events.

* ``task_lookup`` is read by exact name and never listed: handle -> owning
  session, session -> tree root, root -> member sessions, submission reference
  -> owning session, historical alias -> handles, resource -> handles, human ->
  tree roots, and the one-time build marker.
* ``task_open`` holds one small blob per unresolved obligation and is the only
  listing done at boot: open tasks (and terminal ones whose publication is still
  owed), pending mail, workflow pointer repairs and display repairs.

Every record is a derived hint that a tree load re-checks against the event log;
none is the only place a fact lives. Writers keep a superset invariant: an
opening record is put before its event and a closing record is deleted after
its event, so a crash leaves at most a stale or dangling hint, which boot
reconciliation removes. An index may over-claim, never under-claim.

The lookup caches below and the open-record mirror are single-node: this
process is the only writer it trusts. A multi-node deployment needs tree
ownership leases before it can rely on them.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
from collections import OrderedDict
from typing import Any

from clio_agent.arc.store_protocol import ARCStore

LOOKUP_KIND = "task_lookup"
OPEN_KIND = "task_open"
INDEX_VERSION = 1
MARKER = "meta.task_index"
_SAFE = re.compile(r"[A-Za-z0-9_-]{1,96}")
_CACHE_LIMIT = 8192

Row = tuple[str, dict[str, Any]]


def record_key(value: str) -> str:
    """A blob-name-safe, collision-resistant key for an arbitrary identity."""
    if _SAFE.fullmatch(value):
        return value
    return "x" + hashlib.sha256(value.encode()).hexdigest()[:48]


# ---- row builders (shared by online writes and the one-time build) -------


def handle_row(handle: str, session_id: str, kind: str, human_id: str, created_at: str) -> Row:
    """``h.<handle>``: the owning session of one exact handle."""
    return "h." + record_key(handle), {
        "handle": handle,
        "session_id": session_id,
        "kind": kind,
        "human_id": human_id,
        "created_at": created_at,
    }


def session_row(session_id: str, root: str, human_id: str) -> Row:
    """``s.<session>``: the tree root a session belongs to."""
    return "s." + record_key(session_id), {
        "session_id": session_id,
        "root": root,
        "human_id": human_id,
    }


def members_row(root: str, members: list[str]) -> Row:
    """``tree.<root>``: every member session of one tree."""
    return "tree." + record_key(root), {"root": root, "members": sorted(set(members))}


def reference_row(reference: str, handle: str, session_id: str) -> Row:
    """``r.<reference>``: the owning session of an opaque worker reference."""
    return "r." + record_key(reference), {
        "reference": reference,
        "handle": handle,
        "session_id": session_id,
    }


def list_row(prefix: str, identity: str, rows: list[Any]) -> Row:
    """``a.``/``res.``/``hum.``/``wait.`` rows: a short list under one exact identity."""
    return prefix + record_key(identity), {"identity": identity, "rows": rows}


def task_open_row(handle: str, session_id: str, kind: str, human_id: str, created_at: str) -> Row:
    """``t~<handle>``: open work, or terminal work whose publication is owed."""
    return "t~" + record_key(handle), {
        "handle": handle,
        "session_id": session_id,
        "kind": kind,
        "human_id": human_id,
        "created_at": created_at,
    }


def mail_open_row(session_id: str, mail_id: str) -> Row:
    """``m~<session>~<mail>``: accepted mail not yet committed or withdrawn."""
    return "m~" + record_key(session_id) + "~" + record_key(mail_id), {
        "session_id": session_id,
        "mail_id": mail_id,
    }


def workflow_open_row(root_handle: str) -> Row:
    """``w~<root>``: a checkpoint whose owner pointer may still need repair."""
    return "w~" + record_key(root_handle), {"root_handle": root_handle}


def display_open_row(session_id: str, mail_id: str) -> Row:
    """``d~<session>~<mail>``: committed interactive mail whose display is unrepaired."""
    return "d~" + record_key(session_id) + "~" + record_key(mail_id), {
        "session_id": session_id,
        "mail_id": mail_id,
    }


class _Lru:
    """A small bounded mapping for immutable or single-writer lookups."""

    def __init__(self) -> None:
        self._rows: OrderedDict[str, Any] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: str) -> tuple[bool, Any]:
        with self._lock:
            if key not in self._rows:
                return False, None
            self._rows.move_to_end(key)
            return True, self._rows[key]

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            self._rows[key] = value
            self._rows.move_to_end(key)
            while len(self._rows) > _CACHE_LIMIT:
                self._rows.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._rows.clear()


def _encode(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


class TaskIndex:
    """Ordered reads/writes of the two derived record kinds, with lookup caches."""

    def __init__(self, store: ARCStore) -> None:
        self.store = store
        self._handles = _Lru()
        self._sessions = _Lru()
        self._lists = _Lru()
        self._open: dict[str, dict[str, Any]] | None = None
        self._open_lock = threading.Lock()
        self.open_listings = 0

    def clear_caches(self) -> None:
        """Forget cached lookups and the open mirror; clio-core is unchanged."""
        self._handles.clear()
        self._sessions.clear()
        self._lists.clear()
        with self._open_lock:
            self._open = None

    def _get(self, kind: str, name: str) -> Any:
        raw = self.store.get(kind, name)
        return json.loads(raw) if raw else None

    def put_lookup(self, row: Row) -> None:
        """Write one lookup record and refresh this process's cache of it."""
        name, value = row
        self.store.put(LOOKUP_KIND, name, _encode(value))
        if name.startswith("h."):
            self._handles.set(value["handle"], value)
        elif name.startswith("s."):
            self._sessions.set(value["session_id"], value)
        elif "identity" in value:
            self._lists.set(name, list(value["rows"]))

    # ---- task_lookup: point reads only ----------------------------------

    def handle(self, handle: str) -> dict[str, Any] | None:
        """The owning session record of an exact handle, or None."""
        if not handle:
            return None  # a root task's absent parent: no record names it
        found, value = self._handles.get(handle)
        if found:
            return value  # type: ignore[no-any-return]
        name, _ = handle_row(handle, "", "", "", "")
        value = self._get(LOOKUP_KIND, name)
        if isinstance(value, dict) and value.get("handle") == handle:
            self._handles.set(handle, value)
            return value
        return None  # absence is not cached: another writer may add the handle

    def _session(self, session_id: str) -> dict[str, Any] | None:
        found, value = self._sessions.get(session_id)
        if found:
            return value  # type: ignore[no-any-return]
        name, _ = session_row(session_id, "", "")
        value = self._get(LOOKUP_KIND, name)
        if not (isinstance(value, dict) and value.get("session_id") == session_id):
            value = None
        self._sessions.set(session_id, value)
        return value

    def session_known(self, session_id: str) -> bool:
        """Whether a session already joined a task tree."""
        return self._session(session_id) is not None

    def session_root(self, session_id: str) -> str:
        """The tree root of a session; a session with no record is its own root."""
        row = self._session(session_id)
        return str(row["root"]) if row and row.get("root") else session_id

    def members(self, root: str) -> list[str]:
        """Member sessions of a tree, always including its root."""
        name, _ = members_row(root, [])
        row = self._get(LOOKUP_KIND, name)
        members = (
            row.get("members", []) if isinstance(row, dict) and row.get("root") == root else []
        )
        return sorted({root, *(str(member) for member in members)})

    def reference(self, reference: str) -> dict[str, Any] | None:
        """The owning session of an opaque submission reference."""
        if not reference:
            return None
        name, _ = reference_row(reference, "", "")
        row = self._get(LOOKUP_KIND, name)
        return row if isinstance(row, dict) and row.get("reference") == reference else None

    def rows(self, prefix: str, identity: str) -> list[Any]:
        """A short list record (aliases, resource handles, human roots)."""
        name, _ = list_row(prefix, identity, [])
        found, value = self._lists.get(name)
        if found:
            return list(value)
        row = self._get(LOOKUP_KIND, name)
        rows = (
            list(row.get("rows", []))
            if isinstance(row, dict) and row.get("identity") == identity
            else []
        )
        self._lists.set(name, rows)
        return list(rows)

    def marker(self) -> dict[str, Any] | None:
        """The one-time build marker, or None before the records were built."""
        row = self._get(LOOKUP_KIND, MARKER)
        return row if isinstance(row, dict) and row.get("version") == INDEX_VERSION else None

    def put_marker(self, built_at: str) -> None:
        """Write the build marker last, after every derived record."""
        self.store.put(
            LOOKUP_KIND, MARKER, _encode({"version": INDEX_VERSION, "built_at": built_at})
        )

    def done_marker(self, name: str) -> dict[str, Any] | None:
        """A one-time job's completion record (``meta.<name>``), or None if it never finished."""
        row = self._get(LOOKUP_KIND, "meta." + name)
        return row if isinstance(row, dict) else None

    def put_done_marker(self, name: str, value: dict[str, Any]) -> None:
        """Record that a one-time job finished, after everything it wrote."""
        self.store.put(LOOKUP_KIND, "meta." + name, _encode(value))

    # ---- task_open: the small listed tag --------------------------------

    def put_open(self, row: Row) -> None:
        """Open an obligation; called before the event that opens it."""
        name, value = row
        self.store.put(OPEN_KIND, name, _encode(value))
        with self._open_lock:
            if self._open is not None:
                self._open[name] = value

    def delete_open(self, name: str) -> None:
        """Close an obligation; called after the event that closes it."""
        self.store.delete(OPEN_KIND, name)
        with self._open_lock:
            if self._open is not None:
                self._open.pop(name, None)

    def open_records(self, prefix: str = "") -> list[Row]:
        """Open obligations under a name prefix (the tag is listed once per reset)."""
        with self._open_lock:
            mirror = self._open
        if mirror is None:
            listed: dict[str, dict[str, Any]] = {}
            for name, raw in self.store.scan(OPEN_KIND, ""):
                value = json.loads(raw) if raw else None
                if isinstance(value, dict):
                    listed[name] = value
            self.open_listings += 1
            with self._open_lock:
                if self._open is None:
                    self._open = listed
                mirror = self._open
        return sorted(
            (name, dict(value)) for name, value in mirror.items() if name.startswith(prefix)
        )

    def put_many(self, kind: str, rows: list[Row]) -> None:
        """Write many independent records at once (the one-time build only)."""
        from clio_agent.arc.batch_put import PutRecord

        records = [PutRecord(name=name, data=_encode(value)) for name, value in rows]
        for start in range(0, len(records), 256):
            self.store.put_many(kind, records[start : start + 256])
        if kind == OPEN_KIND:
            with self._open_lock:
                self._open = None
        else:
            self.clear_caches()
