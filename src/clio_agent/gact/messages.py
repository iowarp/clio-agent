"""Persistent GACT message ledger for CLIO sessions."""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Optional

from clio_agent.gact.types import Message
from clio_agent.runtime import trace


class LedgerReadError(OSError):
    """A persisted session ledger exists but cannot be read or parsed.

    Distinguishes a genuine read/decode failure (disk error, corrupt JSON, wrong
    top-level shape) from a legitimately-empty or never-persisted ledger. The
    resident-set rehydration path (:mod:`clio_agent.gact.resident_ledgers`) relies
    on that distinction so a transient read blip surfaces a **typed reason** to the
    caller instead of silently installing an empty transcript as truth — which
    would make ``GET /messages`` serve ``[]`` for a session whose full ledger is
    intact on disk (#889 no-silent-fallback; design ``unified-arc-highway.md`` §3.3).
    """


class MessageStore:
    """Thread-safe per-session message persistence.

    Session metadata lives in ``sessions.json``. Message bodies are larger
    and mutate more often, so they are stored as one JSON file per session
    under a sibling ``messages/`` directory.
    """

    def __init__(self, path: Optional[Path] = None) -> None:
        self._path = path
        self._lock = threading.Lock()

    def load_all(self) -> dict[str, list[Message]]:
        """Load all persisted message logs keyed by session id.

        Eager, unbounded whole-store load. Retained for tests and the legacy
        rebuild path; the running server (#889) boots on the index (:meth:`session_ids`)
        and rehydrates one session at a time via :meth:`load_session` instead, so
        the entire transcript corpus never has to be resident at once.
        """

        if self._path is None or not self._path.exists():
            return {}
        rows: dict[str, list[Message]] = {}
        for file_path in self._path.glob("*.json"):
            sid = file_path.stem
            try:
                messages = self._load_file(file_path)
            except LedgerReadError as exc:
                # Boot-time resilience: a single unreadable ledger must not abort the
                # whole-store load, but it is LOGGED (not silently skipped).
                trace.event("MESSAGE-STORE", "load_all skipping unreadable ledger %s: %s", sid, exc)
                continue
            if messages:
                rows[sid] = messages
        return rows

    def session_ids(self) -> list[str]:
        """Return the ids of every persisted session ledger (the boot index).

        Reads directory entries only — never message bodies — so enumerating the
        resident/lazy key space (``__iter__`` / ``list`` / delete-by-id) stays
        O(files), not O(total messages).
        """

        if self._path is None or not self._path.exists():
            return [fp.stem for fp in ()]
        return [fp.stem for fp in self._path.glob("*.json")]

    def has_session(self, session_id: str) -> bool:
        """Whether a persisted ledger exists for ``session_id`` (no body read)."""

        if self._path is None:
            return False
        return self._session_file(session_id).exists()

    def load_session(self, session_id: str) -> Optional[list[Message]]:
        """Load one session's persisted ledger, or ``None`` when it has none.

        The ``None`` return distinguishes *"this session has never been persisted"*
        (a brand-new session whose first append has not landed yet) from *"this
        session exists on disk but its ledger is empty"* (returns ``[]``). The
        resident-set rehydration (:mod:`clio_agent.gact.resident_ledgers`) relies on
        that distinction to decide between a cache-miss ``KeyError`` and an empty
        materialization.
        """

        if self._path is None:
            return None
        file_path = self._session_file(session_id)
        if not file_path.exists():
            return None
        return self._load_file(file_path)

    def iter_session_ledgers(self) -> Iterator[tuple[str, list[Message]]]:
        """Yield ``(session_id, messages)`` one ledger at a time from disk.

        Streaming: each session's bodies are parsed, yielded, and — once the caller
        drops the reference — eligible for collection before the next is read. Used
        by the boot metrics seed (#889) to fold every session into the running
        aggregate without ever holding the whole corpus resident.
        """

        if self._path is None or not self._path.exists():
            return
        for file_path in self._path.glob("*.json"):
            try:
                rows = self._load_file(file_path)
            except LedgerReadError as exc:
                # A corrupt ledger must not crash boot's metrics seed; skip it, but
                # LOUDLY (no silent drop). Its counters are simply absent until it is
                # next written or repaired.
                trace.event(
                    "MESSAGE-STORE",
                    "boot metrics skipping unreadable ledger %s: %s",
                    file_path.stem,
                    exc,
                )
                continue
            yield file_path.stem, rows

    def append(self, session_id: str, message: Message) -> None:
        """Append one message and persist that session's ledger."""

        if self._path is None:
            return
        with self._lock:
            messages = self._load_file(self._session_file(session_id))
            messages.append(message)
            self._flush_locked(session_id, messages)

    def extend(self, session_id: str, messages: list[Message]) -> None:
        """Append several messages and persist that session's ledger."""

        if self._path is None or not messages:
            return
        with self._lock:
            existing = self._load_file(self._session_file(session_id))
            existing.extend(messages)
            self._flush_locked(session_id, existing)

    def replace_session(self, session_id: str, messages: list[Message]) -> None:
        """Replace a session ledger with the supplied chronological messages."""

        if self._path is None:
            return
        with self._lock:
            self._flush_locked(session_id, list(messages))

    def delete_session(self, session_id: str) -> None:
        """Delete a persisted message ledger if it exists."""

        if self._path is None:
            return
        try:
            self._session_file(session_id).unlink()
        except FileNotFoundError:
            return

    def _load_file(self, file_path: Path) -> list[Message]:
        """Parse one session's ledger file into ``Message`` objects.

        A missing file is a legitimately-empty ledger (``[]``). A file that exists
        but cannot be read (disk error), does not parse (corrupt JSON), or is not a
        JSON list raises :class:`LedgerReadError` so callers can distinguish a read
        FAILURE from an empty ledger — the resident set must never cache such a
        failure as an empty transcript (#889). Individual malformed rows inside an
        otherwise-valid list are skipped (row-level format tolerance, unchanged).
        """

        if not file_path.exists():
            return []
        try:
            text = file_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise LedgerReadError(f"cannot read ledger {file_path}: {exc}") from exc
        try:
            raw = json.loads(text)
        except json.JSONDecodeError as exc:
            raise LedgerReadError(f"corrupt ledger JSON in {file_path}: {exc}") from exc
        if not isinstance(raw, list):
            raise LedgerReadError(
                f"ledger {file_path} is not a JSON list (got {type(raw).__name__})"
            )
        messages: list[Message] = []
        for payload in raw:
            if not isinstance(payload, dict):
                continue
            try:
                messages.append(Message(**payload))
            except Exception:  # noqa: BLE001 - malformed persisted message row skipped
                continue
        return messages

    def _flush_locked(self, session_id: str, messages: list[Message]) -> None:
        if self._path is None:
            return
        self._path.mkdir(parents=True, exist_ok=True)
        payload = [m.model_dump(exclude_none=True) for m in messages]
        file_path = self._session_file(session_id)
        tmp = file_path.with_suffix(file_path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, file_path)

    def _session_file(self, session_id: str) -> Path:
        assert self._path is not None
        safe = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in session_id)
        return self._path / f"{safe}.json"
