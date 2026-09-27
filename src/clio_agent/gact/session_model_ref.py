"""The session's model ref follows the model a person picked for it.

A per-message model ref is the person's current choice for the session (the
composer's picker). Accepting the message records it as the session's own
``model`` before the turn runs, so the choice survives whatever the turn does
and every client remount reads it back from the server, instead of living only
in one composer's local state (rel18: a failed turn, or the new-conversation
route becoming the session route, showed "Choose model" again).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from clio_agent.gact.events import Event
from clio_agent.gact.types import ModelRef, Session

if TYPE_CHECKING:  # pragma: no cover
    from fastapi import FastAPI


def remember_session_model(app: "FastAPI", sid: str, current: Any, picked: ModelRef) -> None:
    """Persist ``picked`` as session ``sid``'s model ref and publish the change.

    Args:
        app: The GACT app (session store + event bus).
        sid: The session the message was accepted for.
        current: The session's model ref before this message.
        picked: The accepted per-message model ref.
    """
    wire = picked.model_dump()
    before = current.model_dump() if hasattr(current, "model_dump") else dict(current or {})
    if {key: str(before.get(key) or "") for key in wire} == wire:
        return
    sess = app.state.sessions.update(sid, model=wire)
    if sess is None:
        return
    app.state.bus.publish(
        Event(
            type="session.updated",
            session_id=sid,
            payload=Session(**sess.to_wire()).model_dump(exclude_none=True),
        )
    )


__all__ = ["remember_session_model"]
