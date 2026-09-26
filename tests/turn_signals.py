"""Wait for a turn's real completion signal instead of a wall-clock guess.

A gact turn announces that it settled with a terminal ``session.status_changed``
event, published once the turn runner releases the session's slot (after the
assistant message and completion events are persisted). Tests that must observe
the settled state wait for that event. Polling ``GET /v1/sessions/{sid}`` against a
fixed deadline instead encodes a guess about how long a turn takes: a cold first
turn on a loaded Windows box takes 14 s, so a 10 s (or 2 s) window fails there while
passing on a fast CI runner.
"""

from __future__ import annotations

import threading
from typing import Any

# A hang detector, not a timing budget: the wait returns the moment the event fires,
# and only a turn that never settles runs this long.
TURN_SIGNAL_BACKSTOP_S = 120.0
TERMINAL_STATUSES = frozenset({"idle", "error", "cancelled"})


def terminal_status_after(bus: Any, sid: str, after_event_id: int) -> str | None:
    """Return the first terminal status ``sid`` published after ``after_event_id``, if any."""

    for event in bus.session_events_since(sid, cursor=after_event_id + 1):
        status = (event.payload or {}).get("status")
        if (
            event.session_id == sid
            and event.type == "session.status_changed"
            and status in TERMINAL_STATUSES
        ):
            return str(status)
    return None


def wait_for_terminal_status(
    bus: Any,
    sid: str,
    *,
    after_event_id: int,
    backstop_s: float = TURN_SIGNAL_BACKSTOP_S,
) -> str:
    """Block until ``sid`` publishes a terminal ``session.status_changed`` after a cursor.

    Wakes on the event itself through the bus's synchronous subscription primitive
    (``wait_for_session_events``, which reads the same replay history the SSE feed
    serves), so an event published before this call is still seen. The wait runs on
    a helper thread so a turn that never settles fails at ``backstop_s`` instead of
    hanging the suite.

    Args:
        bus: The app's ``EventBus`` (``app.state.bus``).
        sid: The session whose turn is settling.
        after_event_id: Only events newer than this id count; take it before the
            action that starts or releases the turn (``bus.latest_event_id(sid)``).
        backstop_s: Hang detector; reaching it fails the test.

    Returns:
        The terminal status the event carried (``idle``, ``error`` or ``cancelled``).
    """

    found: list[str] = []

    def watch() -> None:
        while True:
            # Cursor BEFORE the check: an event landing between the two makes the wait
            # below return at once instead of sleeping past it.
            cursor = bus.latest_session_event_id([sid])
            status = terminal_status_after(bus, sid, after_event_id)
            if status is not None:
                found.append(status)
                return
            bus.wait_for_session_events([sid], after_event_id=cursor)

    watcher = threading.Thread(target=watch, name=f"turn-settle-watch-{sid}", daemon=True)
    watcher.start()
    watcher.join(backstop_s)
    assert found, f"session {sid} published no terminal status within the {backstop_s:g}s backstop"
    return found[0]
