"""Which memory a turn used, and the usage counters it moves.

During a turn the tools note what they read (:mod:`.usage`). At the end of the turn the
answer records ``memory_used`` -- the documents read this turn and the snapshot's
profile and preferences, which were loaded -- for the per-answer memory popover, and
the turn's counters are applied on a worker thread, off the turn's path.
"""

from __future__ import annotations

import threading
from typing import Any

from clio_agent.gact.memory.usage import skill_subject

_MEMORY = "memory/"


def note_skill(event: str, skill_id: str, plugin: str) -> None:
    """Count a skill shown in the index (``loaded``) or loaded with ``load_skill`` (``read``)."""
    from clio_agent.gact import context  # noqa: PLC0415

    app = context.active_app()
    usage = getattr(getattr(app, "state", None), "memory_usage", None)
    turn = context.active_turn_id() or context.active_session_id()
    if usage is not None and turn:
        usage.note(turn, skill_subject(plugin, skill_id), event)  # type: ignore[arg-type]


def attach_turn_memory(state: Any) -> None:
    """Record ``memory_used`` on the answer and apply the turn's usage counters."""
    usage = getattr(state.app.state, "memory_usage", None)
    if usage is None:
        return
    read = [s.removeprefix(_MEMORY) for s in usage.reads(state.turn_id) if s.startswith(_MEMORY)]
    session = state.app.state.sessions.get(state.sid)
    snapshot = ((session.metadata or {}) if session is not None else {}).get("memory_snapshot")
    loaded = list(snapshot.get("loaded", [])) if isinstance(snapshot, dict) else []
    if read or loaded:
        state.assistant_metadata["memory_used"] = {"read": read, "loaded": loaded}
    completed = state.error_info is None and not state.cancelled_turn
    threading.Thread(
        target=usage.finish,
        args=(state.turn_id,),
        kwargs={"completed": completed},
        name="memory-usage",
        daemon=True,
    ).start()


__all__ = ["attach_turn_memory", "note_skill"]
