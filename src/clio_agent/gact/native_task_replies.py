"""Which ``tasks/get`` replies are worth a recorded snapshot.

Every poll reply used to record a task snapshot (a clio-core write), so the poll
cadence was the write cadence; with fastmcp's default 5 s ceiling a shell command was
seen as done a median 375 ms after it ended (L1P/L2, 2026-11-04). The shell tool now
polls faster (``tools.shell_poll_interval_ms``), and a reply that changes nothing is
recorded at most once per :data:`REFRESH_S`, the cadence a poll had before, so the
write rate does not grow with the poll rate.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

__all__ = ["REFRESH_S", "reply_status_line", "unchanged_reply"]

#: Longest a running task's snapshot goes unrefreshed while its replies change
#: nothing (fastmcp's default poll ceiling, the cadence before faster polling).
REFRESH_S = 5.0
_COMPARED = ("raw_status", "status", "activity", "progress", "connection_freshness")
#: Replies that may be skipped: a task still running. A terminal reply is always
#: recorded (it carries the result, and settlement reads it).
_RUNNING = frozenset({"working", "input_required"})


def unchanged_reply(current: Any, observed: Any, at: str) -> bool:
    """Whether ``observed`` repeats ``current`` on a running task recorded recently.

    A terminal reply, any changed field, or a snapshot older than :data:`REFRESH_S`
    is recorded. The caller still settles a skipped reply: settlement also acts on
    the task's attempts (a cleanup turned unknown), not only on the reply.
    """
    if current.terminal_at is not None or observed.terminal_at is not None:
        return False
    if observed.raw_status not in _RUNNING:
        return False
    if any(getattr(current, f) != getattr(observed, f) for f in _COMPARED):
        return False
    try:
        age = (
            datetime.fromisoformat(at) - datetime.fromisoformat(current.updated_at)
        ).total_seconds()
    except (TypeError, ValueError):
        return False
    return 0 <= age < REFRESH_S


def reply_status_line(message: str | None) -> str | None:
    """A reply's status message as the task's progress line, or ``None`` to keep it.

    A native shell reports each output chunk through the SDK progress path as a typed
    ``clio.terminal.chunk`` envelope (live terminal deltas decode it), and the task's
    ``statusMessage`` repeats it. That is output, not a status: :class:`NativeProgress`
    records the chunk as task output with its own line ("Shell output available"), so a
    chunk recorded here as the progress message showed raw JSON as the task's status.
    """
    if message is None or not message.startswith("{"):
        return message
    try:
        payload = json.loads(message)
    except ValueError:
        return message
    if isinstance(payload, dict) and payload.get("type") == "clio.terminal.chunk":
        return None
    return message
