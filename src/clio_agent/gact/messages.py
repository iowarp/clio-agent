"""Persistent GACT message ledger for CLIO sessions."""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Optional

from clio_agent.gact.types import Message


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
        """Load all persisted message logs keyed by session id."""

        if self._path is None or not self._path.exists():
            return {}
        rows: dict[str, list[Message]] = {}
        for file_path in self._path.glob("*.json"):
            sid = file_path.stem
            messages = self._load_file(file_path)
            if messages:
                rows[sid] = messages
        return rows

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
        if not file_path.exists():
            return []
        try:
            raw = json.loads(file_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return []
        if not isinstance(raw, list):
            return []
        messages: list[Message] = []
        for payload in raw:
            if not isinstance(payload, dict):
                continue
            try:
                messages.append(Message(**payload))
            except Exception:
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
