"""Where a view's own call is recorded: never inside a turn that happens to be open.

A call a view makes (``tools/call``) or that a surface's ``clio.mcp-app.v1`` opens runs
outside any turn (``mcp_apps._run_bound``), but it binds the session as the tool session
so an external call finds the session's assignment. The tool observer then appended its
rows to whatever turn ledger was open, so a call that landed while the agent was still
answering split the answer's text and was shown, and recorded, as the agent's own call.

Each view call now runs inside :func:`collecting`. The first row it produces decides
where its rows go, once for the whole call: with no turn open, the ordinary no-turn
path, unchanged; with a turn open, they are held and written, when the call ends, as
their own assistant message (``metadata.origin: "mcp_app"``), the way an action card
outside a turn is written. Either way the turn's parts never contain them.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from clio_agent.gact.types import Part

logger = logging.getLogger(__name__)

#: ``metadata.origin`` of the message that holds a view call's rows.
VIEW_CALL_ORIGIN = "mcp_app"


@dataclass
class _ViewCall:
    held: bool | None = None  # undecided until the call's first row
    parts: list[Part] = field(default_factory=list)


_CALL: ContextVar[_ViewCall | None] = ContextVar("clio_view_call_rows", default=None)


def hold(app: Any, sid: str, part: Part) -> bool:
    """Keep ``part`` out of an open turn when it belongs to a view's call.

    Returns ``True`` when the part was taken (the caller appends nothing), ``False``
    for every call that is not a view's, and for a view's call that began with no turn
    open.
    """

    call = _CALL.get()
    if call is None:
        return False
    if call.held is None:
        from clio_agent.gact.tool_observer import _session_turn_transcript  # noqa: PLC0415

        call.held = _session_turn_transcript(app, sid) is not None
    if not call.held:
        return False
    for index, existing in enumerate(call.parts):
        if existing.id and existing.id == part.id:
            call.parts[index] = part  # an update of a row already held
            return True
    call.parts.append(part)
    return True


@contextmanager
def collecting(app: Any, sid: str) -> Iterator[None]:
    """Run one view call; write the rows it held as their own message when it ends."""

    call = _ViewCall()
    token = _CALL.set(call)
    try:
        yield
    finally:
        _CALL.reset(token)
        if call.parts:
            _record(app, sid, call.parts)


def _record(app: Any, sid: str, parts: list[Part]) -> None:
    from clio_agent.gact.events import Event  # noqa: PLC0415
    from clio_agent.gact.runtime.globals import _iso_from_epoch  # noqa: PLC0415
    from clio_agent.gact.session_store import _append_session_message  # noqa: PLC0415
    from clio_agent.gact.types import Message  # noqa: PLC0415

    now = _iso_from_epoch(time.time())
    message = Message(
        id=f"msg_view_{uuid.uuid4().hex[:12]}",
        session_id=sid,
        role="assistant",
        created_at=now,
        updated_at=now,
        parts=parts,
        metadata={"origin": VIEW_CALL_ORIGIN},
    )
    try:
        _append_session_message(app, sid, message)
    except Exception:  # noqa: BLE001 - typed: the rows are reported, never silently lost
        logger.exception("view_call_rows_unrecorded session=%s parts=%d", sid, len(parts))
        return
    app.state.bus.publish(Event(type="message.created", session_id=sid, payload=message.to_wire()))


__all__ = ["VIEW_CALL_ORIGIN", "collecting", "hold"]
