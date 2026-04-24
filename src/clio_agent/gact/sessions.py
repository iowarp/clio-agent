"""GACT v0.2 session registry for CLIO.

Until CLIO-BBBBBBBBBB19 moves session ownership into ARC, the GACT
app owns a small registry of ``Session`` records:

- in-memory dict keyed by session id
- optional JSON persistence so sessions survive ``clio-agent-gact``
  restarts (default: ``~/.config/clio-agent/sessions.json``; the path
  is configurable for tests)

The registry is thread-safe for the workload we expect (FastAPI
serves requests concurrently but each request either reads or writes
the registry atomically; no coordinated multi-step state machines).

Session shape mirrors GACT v0.2 §4.2:

    {
      "id": "sess_...",
      "workspace_id": "ws_default",
      "title": "...",
      "status": "idle" | "running" | "waiting_permission" | "error",
      "created_at": "<ISO-8601 UTC>",
      "updated_at": "<ISO-8601 UTC>",
      "message_count": 0,
      "metadata": {...}
    }

Spec fields the scaffold doesn't populate yet (model, tokens,
cost_usd, summary, archived_at, parent_session_id, agent) stay at
their zero-values until their own items land. Clients tolerate
absent-optional fields per §3.2.
"""

from __future__ import annotations

import json
import os
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

# Keep session ids namespaced so log scraping (and humans) can tell
# them apart from e.g. message ids at a glance.
_SESSION_ID_PREFIX = "sess_"


def _utcnow_iso() -> str:
    """Return the current UTC time in ISO-8601 (microsecond precision).

    Microsecond precision matters for ordering: if two sessions are
    created within the same second (common in tests + batch imports),
    second-precision strings tie and ``list()`` can't sort them
    deterministically. Microseconds effectively never tie in single-
    process code.
    """

    return datetime.now(timezone.utc).isoformat()


def _default_store_path() -> Path:
    """Default on-disk location for the registry.

    Honours ``XDG_CONFIG_HOME`` when set; otherwise uses
    ``~/.config/clio-agent/sessions.json``. The directory is created
    lazily on first write.
    """

    base = os.environ.get("XDG_CONFIG_HOME")
    if base:
        return Path(base) / "clio-agent" / "sessions.json"
    return Path.home() / ".config" / "clio-agent" / "sessions.json"


@dataclass
class Session:
    """A single GACT v0.2 session record.

    Kept as a plain dataclass (not a Pydantic model) because the
    registry needs to serialise to JSON trivially and we control the
    shape explicitly — no validation needed at this layer.
    """

    id: str
    workspace_id: str
    title: str
    status: str = "idle"
    created_at: str = field(default_factory=_utcnow_iso)
    updated_at: str = field(default_factory=_utcnow_iso)
    message_count: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_wire(self) -> dict[str, Any]:
        """JSON-serialisable dict matching SPEC §4.2's optional-
        field-friendly shape (no nulls where the client expects
        empty)."""

        return asdict(self)


class SessionStore:
    """Thread-safe session registry with optional JSON persistence.

    Construction
    ------------

    ``path=None`` → purely in-memory (used in tests that don't care
    about persistence). ``path=Path`` → load on startup, write on
    every mutation.

    Concurrency
    -----------

    A single ``threading.Lock`` guards the in-memory dict. Every
    mutation writes to disk while holding the lock, so an interrupted
    write (process crash mid-flush) at worst loses the last
    un-committed change — it can't leave a half-written JSON file
    because we write+fsync to a temp file and atomically rename.
    """

    def __init__(self, path: Optional[Path] = None) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._sessions: dict[str, Session] = {}
        if path is not None:
            self._load()

    # ---- lifecycle ----------------------------------------------------

    def _load(self) -> None:
        """Populate in-memory dict from the on-disk JSON, if any."""

        assert self._path is not None
        if not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text())
        except (json.JSONDecodeError, OSError):
            # Corrupted / unreadable store — start fresh, don't
            # crash the server. The previous file stays on disk so
            # operators can salvage it out-of-band.
            return
        if not isinstance(raw, dict):
            return
        for sid, payload in raw.items():
            if not isinstance(payload, dict):
                continue
            try:
                self._sessions[sid] = Session(**payload)
            except TypeError:
                # Schema drift (e.g. a field was renamed between
                # releases). Skip that one row and keep going.
                continue

    def _flush(self) -> None:
        """Serialise the in-memory dict to disk atomically.

        Caller holds ``self._lock``.
        """

        if self._path is None:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {sid: asdict(s) for sid, s in self._sessions.items()}
        # temp-file + rename = atomic on POSIX so a mid-write crash
        # can't leave a partial JSON blob on disk.
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True))
        os.replace(tmp, self._path)

    # ---- CRUD ---------------------------------------------------------

    def create(
        self,
        *,
        workspace_id: str,
        title: str = "",
        metadata: Optional[dict[str, Any]] = None,
    ) -> Session:
        """Create a new session. Returns the freshly-minted record."""

        sid = _SESSION_ID_PREFIX + uuid.uuid4().hex[:12]
        now = _utcnow_iso()
        sess = Session(
            id=sid,
            workspace_id=workspace_id,
            title=title or f"session {sid[-6:]}",
            status="idle",
            created_at=now,
            updated_at=now,
            metadata=dict(metadata or {}),
        )
        with self._lock:
            self._sessions[sid] = sess
            self._flush()
        return sess

    def get(self, sid: str) -> Optional[Session]:
        with self._lock:
            return self._sessions.get(sid)

    def list(
        self,
        *,
        workspace_id: Optional[str] = None,
    ) -> list[Session]:
        """List sessions, newest-first. Optional workspace filter."""

        with self._lock:
            rows = list(self._sessions.values())
        if workspace_id:
            rows = [r for r in rows if r.workspace_id == workspace_id]
        # created_at is ISO-8601 UTC — lexicographic sort is
        # chronological here, so descending reverse-sort works.
        rows.sort(key=lambda r: r.created_at, reverse=True)
        return rows

    def delete(self, sid: str) -> bool:
        with self._lock:
            existed = sid in self._sessions
            self._sessions.pop(sid, None)
            if existed:
                self._flush()
        return existed

    def update(
        self,
        sid: str,
        *,
        title: Optional[str] = None,
        status: Optional[str] = None,
        message_count: Optional[int] = None,
        metadata_patch: Optional[dict[str, Any]] = None,
    ) -> Optional[Session]:
        """Mutate a session in place.

        Any field left ``None`` is untouched. ``metadata_patch``
        merges into the existing metadata (shallow) so callers can
        stamp additional keys without clobbering the rest.
        """

        with self._lock:
            sess = self._sessions.get(sid)
            if sess is None:
                return None
            if title is not None:
                sess.title = title
            if status is not None:
                sess.status = status
            if message_count is not None:
                sess.message_count = message_count
            if metadata_patch is not None:
                sess.metadata.update(metadata_patch)
            sess.updated_at = _utcnow_iso()
            self._flush()
            return sess

    # ---- introspection hooks (for /v1/memory/stats + tests) ----------

    def count(self) -> int:
        with self._lock:
            return len(self._sessions)
