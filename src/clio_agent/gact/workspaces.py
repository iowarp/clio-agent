"""Workspace registry — the parent scope of sessions (SPEC §4.1).

A workspace pins a filesystem root the agent's tools are allowed
to touch + isolates the session bucket. Persisted to disk so the
TUI's sidebar reflects what the user added across restarts.

Storage shape mirrors ``sessions.py`` for consistency: dataclass
records, threading.Lock, atomic temp-file flush. Every CLIO install
gets a default workspace at boot if none exist, so deploying a
fresh server doesn't strand the TUI in a "no workspaces" empty
state.
"""

from __future__ import annotations

import os
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

_WORKSPACE_ID_PREFIX = "ws_"


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _default_store_path() -> Path:
    """Same XDG-friendly resolution sessions.py uses, sibling file."""

    base = os.environ.get("XDG_CONFIG_HOME")
    if base:
        return Path(base) / "clio-agent" / "workspaces.json"
    return Path.home() / ".config" / "clio-agent" / "workspaces.json"


@dataclass
class Workspace:
    """A single workspace record."""

    id: str
    name: str
    root_path: str = ""
    created_at: str = field(default_factory=_utcnow_iso)
    updated_at: str = field(default_factory=_utcnow_iso)
    config: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_wire(self) -> dict[str, Any]:
        return asdict(self)


class WorkspaceStore:
    """Thread-safe workspace registry with optional JSON persistence.

    Construction
    ------------

    ``path=None`` → purely in-memory (used in tests). ``path=Path``
    → load on startup, atomic temp+rename on every mutation.

    Default workspace
    -----------------

    On load, if no workspaces exist (fresh install or missing file),
    a default ``ws_default`` workspace is materialised so the TUI
    always sees at least one row + sessions can pick a parent.
    """

    def __init__(self, *, path: Optional[Path] = None) -> None:
        self._lock = threading.Lock()
        self._path = path
        self._workspaces: dict[str, Workspace] = {}
        self._load()
        if not self._workspaces:
            self._seed_default()

    # ---- persistence -------------------------------------------------

    def _load(self) -> None:
        if self._path is None or not self._path.exists():
            return
        try:
            import json

            data = json.loads(self._path.read_text(encoding="utf-8"))
        except Exception:
            return
        for row in data.get("workspaces", []):
            try:
                ws = Workspace(**row)
                self._workspaces[ws.id] = ws
            except Exception:
                continue

    def _flush(self) -> None:
        if self._path is None:
            return
        import json

        self._path.parent.mkdir(parents=True, exist_ok=True)
        rows = [w.to_wire() for w in self._workspaces.values()]
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(
            json.dumps({"workspaces": rows}, indent=2),
            encoding="utf-8",
        )
        tmp.replace(self._path)

    def _seed_default(self) -> None:
        ws = Workspace(
            id="ws_default",
            name="default",
            root_path=os.getcwd(),
        )
        self._workspaces[ws.id] = ws
        self._flush()

    # ---- public API --------------------------------------------------

    def create(
        self,
        *,
        name: str,
        root_path: str = "",
        metadata: Optional[dict[str, Any]] = None,
    ) -> Workspace:
        """Create a new workspace + persist."""

        wid = _WORKSPACE_ID_PREFIX + uuid.uuid4().hex[:12]
        now = _utcnow_iso()
        ws = Workspace(
            id=wid,
            name=name or wid[-6:],
            root_path=root_path,
            created_at=now,
            updated_at=now,
            metadata=dict(metadata or {}),
        )
        with self._lock:
            self._workspaces[wid] = ws
            self._flush()
        return ws

    def get(self, wid: str) -> Optional[Workspace]:
        with self._lock:
            return self._workspaces.get(wid)

    def list(self) -> list[Workspace]:
        with self._lock:
            return sorted(
                self._workspaces.values(),
                key=lambda w: w.created_at,
                reverse=True,
            )

    def delete(self, wid: str) -> bool:
        """Remove a workspace. Returns True if it existed.

        Refuses to delete ``ws_default`` so the system always has at
        least one workspace + the seeding invariant holds.
        """

        if wid == "ws_default":
            return False
        with self._lock:
            existed = wid in self._workspaces
            self._workspaces.pop(wid, None)
            self._flush()
        return existed

    def update(
        self,
        wid: str,
        *,
        name: Optional[str] = None,
        root_path: Optional[str] = None,
        metadata_patch: Optional[dict[str, Any]] = None,
    ) -> Optional[Workspace]:
        with self._lock:
            ws = self._workspaces.get(wid)
            if ws is None:
                return None
            if name is not None:
                ws.name = name
            if root_path is not None:
                ws.root_path = root_path
            if metadata_patch is not None:
                ws.metadata.update(metadata_patch)
            ws.updated_at = _utcnow_iso()
            self._flush()
            return ws

    def count(self) -> int:
        with self._lock:
            return len(self._workspaces)
