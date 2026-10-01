"""The ``injection`` part: harness data the agent was given, shown as what it is.

Everything CLIO adds to what the agent sees -- turn additions (plan reminder, todos,
replan, memory hits, finished children's results), and notes on a tool call (a path
hint, a circuit-breaker warning, an oversize result's file) -- is recorded in the
transcript as one ``injection`` part with the exact text the agent got. Clients
render it distinctly (a syringe icon), so the user always sees when the agent
received harness data. Built like :mod:`clio_agent.gact.action_cards`: a pure
builder and a turn-agnostic emitter.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from clio_agent.gact.parts import Part

logger = logging.getLogger(__name__)

__all__ = ["emit_injection", "injection_part"]

#: Typed reason when an injection could not reach a frozen transcript.
INJECTION_TRANSCRIPT_FROZEN = "injection_transcript_frozen"


def injection_part(source: str, text: str, *, call_id: str = "", agent_id: str = "") -> Part:
    """Build one ``injection`` part (``call_id``: the tool call it is about, if any)."""
    from clio_agent.gact import context as _ctx  # noqa: PLC0415

    # Inside a variant try the part belongs to the try's tab (its variants_id / try_index).
    metadata: dict[str, Any] = _ctx.stamp_active_try({"actor": "algorithm"})
    if call_id:
        metadata["call_id"] = call_id
    return Part(
        id=f"live_injection_{uuid.uuid4().hex[:12]}",
        type="injection",
        agent_id=agent_id,
        source=source,
        text=text,
        metadata=metadata,
    )


def emit_injection(source: str, text: str, *, call_id: str = "", agent_id: str = "") -> bool:
    """Append an injection to the active session's live transcript.

    Returns ``False`` when there is no app session (a bare loop, the CLI: nothing
    to show it in) or the turn's transcript is already frozen (logged typed).
    """
    from clio_agent.gact import context as _ctx  # noqa: PLC0415
    from clio_agent.gact.tool_observer import (  # noqa: PLC0415
        _append_live_assistant_part,
        _mirror_transcript_state,
        _session_turn_transcript,
    )

    app = _ctx.active_app()
    session_id = _ctx.active_session_id()
    if app is None or not session_id or not text:
        return False
    part = injection_part(source, text, call_id=call_id, agent_id=agent_id)
    transcript = _session_turn_transcript(app, session_id)
    if transcript is None:
        _append_live_assistant_part(app, session_id, part)
        return True
    if transcript.append_part(part) is None:
        logger.warning(
            "injection dropped reason=%s session=%s source=%s",
            INJECTION_TRANSCRIPT_FROZEN,
            session_id,
            source,
        )
        return False
    _mirror_transcript_state(app, session_id, transcript)
    return True
