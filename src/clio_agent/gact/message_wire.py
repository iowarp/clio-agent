"""Read-boundary single-representation normalization for the message ledger.

#732 / epic #880 (S2). The ReAct step's ``next_thought`` has a single approved
home: its VISIBLE streamed ``text`` row (a ``text`` part tagged
``metadata["signature_field_name"] == "next_thought"``). The copy the tool
observer also stamps onto the sibling ``tool_call`` part's ``thought`` is the
redundant duplication. The live observer clears that copy at emit time (see
``tool_observer._make_tool_observer``), so freshly written assistant messages
already carry ``tool_call.thought == ""`` whenever a visible row exists.

Messages persisted BEFORE S2 still hold BOTH: a ``next_thought`` text part AND a
populated ``tool_call.thought``. ``GET /v1/sessions/{sid}/messages`` (and the
per-message drill-down) serve their parts verbatim via ``Message.to_wire``, so a
client that renders ``tool_call.thought`` verbatim (as it now must, the client
dedup having been deleted) would show the thought TWICE on reload of that old
data. This module closes that gap at the READ boundary.

The gate is an OP-IDENTITY PRESENCE check — "does this SAME message contain a
same-agent ``next_thought`` text row?" — never a string comparison. It mirrors
the observer's ``TurnTranscript.streamed_field_started`` gate; the three prose
heuristics deleted in S2 are NOT reintroduced here. Both this module and the live
observer therefore key single-representation off the presence of the visible row,
not off the text it carries.

No silent fallback: every clear emits the same structured ``stream_audit`` reason
(``next_thought_owns_visible_text_row``) the live gate emits, tagged
``origin="message_read"`` so reload-time normalization is distinguishable in the
audit trail from the live-emit clear. A message with no visible ``next_thought``
row (the SDK/batch-gap shape, scenario B) is returned UNCHANGED — its
``tool_call.thought`` is that thought's only home and is kept.
"""

from __future__ import annotations

from clio_agent.gact.types import Message, Part
from clio_agent.runtime.stream_audit import stream_audit


def _visible_next_thought_agents(parts: list[Part]) -> set[str]:
    """Agents that emitted a non-empty visible ``next_thought`` text row here.

    Op-identity presence: a ``text`` part carrying
    ``metadata["signature_field_name"] == "next_thought"`` with non-blank text is
    the visible row that owns the thought for its agent. Blank rows do not count
    (an empty-after-clean row is dropped from the ledger and owns nothing).
    """

    agents: set[str] = set()
    for part in parts:
        if (
            part.type == "text"
            and (part.text or "").strip()
            and part.metadata.get("signature_field_name") == "next_thought"
        ):
            agents.add(part.agent_id)
    return agents


def normalize_thought_ownership(message: Message) -> Message:
    """Return ``message`` with redundant ``tool_call.thought`` copies cleared.

    A ``tool_call`` part's ``thought`` is cleared IFF the SAME message already
    carries a same-agent visible ``next_thought`` text row (op-identity presence;
    never a string compare). When nothing needs clearing — no visible row, or no
    tool_call carrying a same-agent copy (the new-session shape, where the live
    observer already emptied it) — the original message is returned unchanged, so
    this is a no-op for post-S2 data and only repairs pre-S2 persisted sessions.

    Each clear emits a ``stream_audit`` ``bridge.tool_thought`` record with
    ``duplicate_reason="next_thought_owns_visible_text_row"`` and
    ``origin="message_read"`` (no silent drop).
    """

    parts = message.parts
    owned = _visible_next_thought_agents(parts)
    if not owned:
        return message

    clear_ids = {
        part.id
        for part in parts
        if part.type == "tool_call"
        and (part.thought or "").strip()
        and part.agent_id in owned
    }
    if not clear_ids:
        return message

    new_parts: list[Part] = []
    for part in parts:
        if part.id in clear_ids:
            stream_audit(
                "bridge.tool_thought",
                agent_id=part.agent_id,
                field="next_thought",
                visible=False,
                duplicate_suppressed=True,
                duplicate_reason="next_thought_owns_visible_text_row",
                origin="message_read",
                head=(part.thought or "")[:120],
            )
            new_parts.append(part.model_copy(update={"thought": ""}))
        else:
            new_parts.append(part)
    return message.model_copy(update={"parts": new_parts})
