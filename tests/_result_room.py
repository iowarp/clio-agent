"""Pin the room one tool result has (:mod:`clio_agent.tools.result_room`) in a test.

The room is measured from the calling model's context window; a test without an agent
loop has none. ``pin_result_room`` makes every measurement in the process (HTTP test
clients and worker threads included) return a room of exactly ``chars`` characters.
"""

from __future__ import annotations

import pytest

from clio_agent.tools import result_room as result_room_module
from clio_agent.tools.result_room import CHARS_PER_TOKEN, ResultRoom


def pin_result_room(monkeypatch: pytest.MonkeyPatch, chars: int) -> ResultRoom:
    """Make the room ``chars`` characters (``chars // 4`` tokens) for this test."""
    room = ResultRoom(tokens=chars // CHARS_PER_TOKEN, chars=chars, reason="pinned")
    monkeypatch.setattr(result_room_module, "_measure", lambda: room)
    return room
