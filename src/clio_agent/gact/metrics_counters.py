"""This server's message rollups for ``GET /v1/metrics`` (#770 Wave-C C3).

A poll reads running counters instead of re-walking history. The counters cover the
messages this process recorded since it started -- by role, and the recent tool-call
durations (a bounded window per key) behind the latency percentiles -- like
``uptime_s``. Nothing is replayed at startup: the all-time message total comes from
each session's persisted ``message_count``, and clio-core holds the messages
themselves.
"""

from __future__ import annotations

from collections import Counter, deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from clio_agent.gact.types import Message

#: Tool-call durations kept per latency key: the percentiles describe recent calls.
LATENCY_WINDOW = 1000


@dataclass
class MetricsCounters:
    """Message rollups for this process, kept by the session_store write seams."""

    by_role: Counter[str] = field(default_factory=Counter)
    latency_samples: dict[str, deque[float]] = field(default_factory=dict)

    def add_message(self, session_id: str, message: "Message") -> None:
        """Fold one newly recorded message into this server's rollups."""

        del session_id
        self.by_role[getattr(message, "role", "") or ""] += 1
        for key, vals in _message_samples(message).items():
            self.latency_samples.setdefault(key, deque(maxlen=LATENCY_WINDOW)).extend(vals)

    def add_messages(self, session_id: str, messages: list["Message"]) -> None:
        """Fold several newly recorded messages into this server's rollups."""

        for message in messages:
            self.add_message(session_id, message)

    def role_counts(self) -> dict[str, int]:
        """Non-zero per-role counts of the messages this server recorded."""

        return {role: n for role, n in self.by_role.items() if n > 0}


def conversation_count(messages: list["Message"]) -> int:
    """A session's ``message_count``: its messages less command confirmations."""

    return sum(1 for message in messages if not _is_command_result(message))


def counts_toward_conversation(message: "Message") -> bool:
    """Whether a newly recorded message adds one to its session's ``message_count``."""

    return not _is_command_result(message)


def _is_command_result(message: "Message") -> bool:
    metadata: Any = getattr(message, "metadata", None) or {}
    return isinstance(metadata, dict) and metadata.get("synthetic") == "command_result"


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
