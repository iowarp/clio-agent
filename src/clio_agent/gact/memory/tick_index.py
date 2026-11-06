"""Which sessions a background memory tick reads, chosen from the session index.

:func:`~clio_agent.gact.memory.background.memory_tick` runs every scheduler minute.
Reading every session record each time made a long-lived store's whole history pass
through clio-core once a minute. The session index carries what the tick decides on
first (kind, status, last interaction, an open pass) plus :func:`settled_reason`, the
skip the session's own record already settles until it changes. A tick then reads
only:

* the sessions with a pass in flight (``memory_pass_open``), to collect them;
* the root conversations and forks that are idle by status and by their last
  interaction, and whose skip is not settled.

A settled session is counted under its reason from the index, without a read. The
verdict a tick records (``metadata.memory_checked``) is keyed by what changes when the
conversation does, so excluding a settled session never skips a pass that is due.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any

__all__ = ["settled_reason", "tick_sessions"]

#: Stored statuses of a session the pass may read (``skip_reason``'s idle statuses).
_IDLE_STATUSES = frozenset({"idle", "error", "cancelled"})
_CONVERSATION_KINDS = frozenset({"conversation", "branch"})


def settled_reason(session: Any) -> str:
    """The skip ``session``'s own record settles until it changes (``""``: none).

    The value of the ``memory_settled`` index column: the session opted out of memory,
    is a curator, or carries a transcript verdict (``memory_checked``) that still
    matches its message count, last interaction, watermark and last pass. A verdict is
    keyed with the minimum of user turns it was judged against.
    """
    from clio_agent.gact.memory.background import (  # noqa: PLC0415 - import cycle
        CHECKED,
        TRANSCRIPT_REASONS,
        _checked,
    )
    from clio_agent.gact.memory.policy import SESSION_CURATOR, SESSION_MEMORY_OFF  # noqa: PLC0415

    metadata = session.metadata if isinstance(session.metadata, Mapping) else {}
    if metadata.get(SESSION_MEMORY_OFF):
        return "session_memory_off"
    if metadata.get(SESSION_CURATOR):
        return "curator_session"
    checked = metadata.get(CHECKED)
    if not isinstance(checked, Mapping):
        return ""
    reason = str(checked.get("reason") or "")
    minimum = checked.get("minimum_user_messages")
    if reason in TRANSCRIPT_REASONS and dict(checked) == _checked(session, reason, minimum):
        return f"{reason}@{minimum}"
    return ""


def tick_sessions(
    app: Any, now: datetime, idle: timedelta, minimum: int
) -> tuple[list[Any], dict[str, int]]:
    """The session rows this tick reads, and the settled sessions counted by reason.

    Args:
        app: The GACT app (its session store).
        now: The tick's time.
        idle: How long a conversation stays untouched before it is due.
        minimum: The minimum of fresh user turns the pass needs now.
    """
    from clio_agent.gact.memory.background import TRANSCRIPT_REASONS  # noqa: PLC0415
    from clio_agent.gact.session_records import PageQuery  # noqa: PLC0415

    sessions = app.state.sessions
    open_passes = sessions.ids(PageQuery(archived=None, memory_pass_open=True))
    settled = {"session_memory_off", "curator_session"} | {
        f"{reason}@{minimum}" for reason in TRANSCRIPT_REASONS
    }
    idle_query = PageQuery(
        archived=None,
        kinds=_CONVERSATION_KINDS,
        statuses=_IDLE_STATUSES,
        recent_before=(now - idle).isoformat(),
        memory_pass_open=False,
    )
    counted: dict[str, int] = {}
    due: list[str] = []
    for row in sessions.columns(idle_query):
        if row.memory_settled in settled:
            key = row.memory_settled.split("@", 1)[0]
            counted[key] = counted.get(key, 0) + 1
        else:
            due.append(row.id)
    ids = list(dict.fromkeys([*open_passes, *due]))
    return sessions.select(PageQuery(archived=None, ids=frozenset(ids))), counted
