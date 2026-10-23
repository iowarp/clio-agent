"""Usage counters for memory documents and skills (``memory_usage`` records).

Each subject -- ``memory/<document name>`` or ``skill/<plugin>/<skill>`` -- counts how
often it was ``loaded`` (in a session's memory snapshot or skill index), ``read``
(``memory_read``, or ``load_skill``), and ``read_in_success`` (read in a turn that
completed and has no negative rating), plus ``last_read``. Counters are their own kind
so updating one never re-tokenizes a document.

A turn's events are buffered in memory and applied once at turn end, off the turn's
hot path. A rating that arrives later moves ``read_in_success`` for the documents that
turn read (:meth:`UsageCounters.rate`).
"""

from __future__ import annotations

import json
import threading
from collections import Counter
from dataclasses import asdict, dataclass, field
from typing import Literal

from clio_agent.arc.memory_document import now_stamp
from clio_agent.arc.store_protocol import ARCStore
from clio_agent.gact.memory.store import USAGE

Event = Literal["loaded", "read"]


def document_subject(name: str) -> str:
    """The usage subject of a memory document."""
    return f"memory/{name}"


def skill_subject(plugin: str, skill: str) -> str:
    """The usage subject of a skill."""
    return f"skill/{plugin or '-'}/{skill}"


@dataclass
class Usage:
    """One subject's counters."""

    loaded: int = 0
    read: int = 0
    read_in_success: int = 0
    last_read: str = ""


@dataclass
class _Turn:
    loaded: Counter[str] = field(default_factory=Counter)
    read: Counter[str] = field(default_factory=Counter)


class UsageCounters:
    """Buffers a turn's usage and applies it to the store at turn end."""

    def __init__(self, store: ARCStore) -> None:
        self._store = store
        self._turns: dict[str, _Turn] = {}
        self._buffer_lock = threading.Lock()
        self._write_lock = threading.Lock()

    def note(self, turn: str, subject: str, event: Event) -> None:
        """Record that ``turn`` loaded or read ``subject`` (cheap, in memory)."""
        with self._buffer_lock:
            buffered = self._turns.setdefault(turn, _Turn())
            getattr(buffered, event)[subject] += 1

    def reads(self, turn: str) -> tuple[str, ...]:
        """Subjects ``turn`` has read so far."""
        with self._buffer_lock:
            buffered = self._turns.get(turn)
            return tuple(buffered.read) if buffered else ()

    def get(self, subject: str) -> Usage:
        """The stored counters of ``subject``."""
        raw = self._store.get(USAGE, subject)
        return Usage(**json.loads(raw)) if raw else Usage()

    def _apply(self, subject: str, *, loaded: int = 0, read: int = 0, success: int = 0) -> None:
        usage = self.get(subject)
        usage.loaded += loaded
        usage.read += read
        usage.read_in_success = max(0, usage.read_in_success + success)
        if read:
            usage.last_read = now_stamp()
        self._store.put(USAGE, subject, json.dumps(asdict(usage)).encode("utf-8"))

    def finish(self, turn: str, *, completed: bool) -> None:
        """Apply ``turn``'s buffered usage; its reads count as successful if it completed.

        Called at turn end, from a worker thread.
        """
        with self._buffer_lock:
            buffered = self._turns.pop(turn, None)
        if buffered is None:
            return
        with self._write_lock:
            for subject in sorted(set(buffered.loaded) | set(buffered.read)):
                reads = buffered.read.get(subject, 0)
                self._apply(
                    subject,
                    loaded=buffered.loaded.get(subject, 0),
                    read=reads,
                    success=reads if completed else 0,
                )

    def rate(self, subjects: tuple[str, ...], *, became_bad: bool) -> None:
        """Move ``read_in_success`` when a turn that read ``subjects`` is rated.

        Args:
            subjects: What the rated turn read (its ``memory_used`` record).
            became_bad: ``True`` when the turn's rating turned negative, ``False``
                when a negative rating was withdrawn or replaced.
        """
        with self._write_lock:
            for subject in subjects:
                self._apply(subject, success=-1 if became_bad else 1)


__all__ = ["Usage", "UsageCounters", "document_subject", "skill_subject"]
