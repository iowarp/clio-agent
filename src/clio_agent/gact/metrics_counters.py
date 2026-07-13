"""Running aggregate for ``GET /v1/metrics`` (#770 Wave-C C3).

The metrics endpoint used to re-walk *every message of every session* (and every
tool call within them) on every poll to rebuild the message-count / by-role /
tool-latency rollups. The TUI polls it regularly, so that walk was O(total
messages + total tool calls) per request.

This module maintains those message-derived aggregates **incrementally** at the
single message write seam (:mod:`clio_agent.gact.session_store`), so a poll reads
a running counter in O(number of latency buckets) instead of re-walking history.
The values are byte-identical to the old full walk: ``_latency_stat`` sorts its
samples, so sample *order* is irrelevant, and the same multiset of durations is
accumulated.

Per-session contributions are retained so a whole-ledger replace (compaction) or
a session delete can subtract that session's samples back out exactly, keeping
the global aggregate correct without re-walking the survivors.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from clio_agent.gact.types import Message


@dataclass
class _SessionAggregate:
    """One session's contribution to the global metrics counters."""

    count: int = 0
    by_role: Counter[str] = field(default_factory=Counter)
    latency_samples: dict[str, list[float]] = field(default_factory=dict)


@dataclass
class MetricsCounters:
    """Running, seam-maintained aggregate for the metrics message rollups.

    Not persisted: rebuilt from the loaded message store at boot
    (:meth:`rebuild`) and kept live by the session_store write seams.
    """

    message_total: int = 0
    by_role: Counter[str] = field(default_factory=Counter)
    latency_samples: dict[str, list[float]] = field(default_factory=dict)
    _per_session: dict[str, _SessionAggregate] = field(default_factory=dict)

    # ---- rebuild / seam hooks ------------------------------------------- #

    def rebuild(self, messages: dict[str, list["Message"]]) -> None:
        """Recompute the whole aggregate from a full message map (boot only)."""

        self.message_total = 0
        self.by_role = Counter()
        self.latency_samples = {}
        self._per_session = {}
        for session_id, rows in messages.items():
            self.set_session(session_id, rows)

    def add_message(self, session_id: str, message: "Message") -> None:
        """Fold one appended message into the aggregate."""

        agg = self._per_session.setdefault(session_id, _SessionAggregate())
        role = getattr(message, "role", "") or ""
        samples = _message_samples(message)
        agg.count += 1
        agg.by_role[role] += 1
        self.message_total += 1
        self.by_role[role] += 1
        for key, vals in samples.items():
            agg.latency_samples.setdefault(key, []).extend(vals)
            self.latency_samples.setdefault(key, []).extend(vals)

    def add_messages(self, session_id: str, messages: list["Message"]) -> None:
        """Fold several appended messages into the aggregate."""

        for message in messages:
            self.add_message(session_id, message)

    def set_session(self, session_id: str, messages: list["Message"]) -> None:
        """Replace a session's contribution (whole-ledger replace / initial set)."""

        self.remove_session(session_id)
        for message in messages:
            self.add_message(session_id, message)

    def remove_session(self, session_id: str) -> None:
        """Subtract a session's contribution back out (delete / pre-replace)."""

        agg = self._per_session.pop(session_id, None)
        if agg is None:
            return
        self.message_total -= agg.count
        self.by_role.subtract(agg.by_role)
        for role in [r for r, n in list(self.by_role.items()) if n <= 0]:
            del self.by_role[role]
        for key, vals in agg.latency_samples.items():
            merged = self.latency_samples.get(key)
            if not merged:
                continue
            remaining = Counter(merged)
            remaining.subtract(Counter(vals))
            rebuilt = [v for v, n in remaining.items() for _ in range(max(0, n))]
            if rebuilt:
                self.latency_samples[key] = rebuilt
            else:
                self.latency_samples.pop(key, None)

    # ---- read side ------------------------------------------------------ #

    def role_counts(self) -> dict[str, int]:
        """Non-zero per-role message counts (wire ``messages.by_role``)."""

        return {role: n for role, n in self.by_role.items() if n > 0}


def _message_samples(message: "Message") -> dict[str, list[float]]:
    """Extract the positive tool-call durations from one message's metadata.

    Keys mirror the historical full-walk: ``tool:{name}`` per tool plus an
    overall ``tool_call`` bucket. Non-numeric or non-positive durations dropped.
    """

    metadata: Any = getattr(message, "metadata", None) or {}
    calls = metadata.get("tools_called") if isinstance(metadata, dict) else None
    samples: dict[str, list[float]] = {}
    for call in calls or []:
        if not isinstance(call, dict):
            continue
        dur = call.get("duration_ms")
        if not isinstance(dur, (int, float)) or dur <= 0:
            continue
        name = str(call.get("name") or call.get("tool") or "tool")
        samples.setdefault(f"tool:{name}", []).append(float(dur))
        samples.setdefault("tool_call", []).append(float(dur))
    return samples
