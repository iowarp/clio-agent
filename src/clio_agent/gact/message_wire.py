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

The gate is a PER-STEP POSITIONAL consume that mirrors the live observer (#883):
a surviving ``next_thought`` text row marks its agent pending; the NEXT same-agent
``tool_call`` consumes it and is cleared iff it carried a copy; every ``tool_call``
ends its agent's step. Survival is the shared ``thought_dedup.survives_clean``
kernel (with ``read_boundary_clean``) both paths call, so live and reload cannot
drift across the ``_clean_text`` boundary — the exact divergence #883 names. A
format-only "does this clean to empty?" test, never a prose comparison; the three
S2 heuristics are NOT reintroduced.

No silent fallback: every clear emits ``next_thought_owns_visible_text_row`` and
every meaningful KEEP (a rowless copy whose agent owned a row elsewhere in the
message — the over-clear the old set logic would have made)
``thought_kept_no_surviving_next_thought_row``, both tagged ``origin="message_read"``.
A plain no-row message (scenario B) is returned UNCHANGED — its
``tool_call.thought`` is that thought's only home and is kept.
"""

from __future__ import annotations

from clio_agent.gact.thought_dedup import (
    REASON_OWNS_ROW,
    REASON_RELOAD_KEEP,
    TOOL_THOUGHT_STAGE,
    read_boundary_clean,
    survives_clean,
)
from clio_agent.gact.types import Message, Part
from clio_agent.runtime.stream_audit import stream_audit


def normalize_thought_ownership(message: Message) -> Message:
    """Clear each tool_call.thought that its OWN ReAct step's next_thought row owns.

    Per-step POSITIONAL consume mirroring the live gate: walking parts in order, a
    surviving next_thought text row marks its agent 'pending'; the NEXT same-agent
    tool_call consumes it and is cleared iff it carried a copy. A tool_call ALWAYS
    ends its agent's step (resets pending) even when its thought was already blanked
    live — so a later ROWLESS step (marker-only / SDK-gap) is KEPT, matching live.
    Survival uses the shared ``survives_clean`` kernel with ``read_boundary_clean``
    so a pre-S2 raw marker-only row cleans to empty and does not falsely own a step.
    """

    parts = message.parts
    pending: dict[str, bool] = {}  # agent -> current step owns a surviving row
    owned_any: dict[str, bool] = {}  # agent -> owned a surviving row anywhere
    clear_ids: set[str] = set()
    kept_calls: list[Part] = []  # rowless non-blank tool_calls, for the KEEP audit
    for part in parts:
        if (
            part.type == "text"
            and part.metadata.get("signature_field_name") == "next_thought"
            and survives_clean(part.text or "", read_boundary_clean)
        ):
            pending[part.agent_id] = True
            owned_any[part.agent_id] = True
        elif part.type == "tool_call":
            if pending.get(part.agent_id) and (part.thought or "").strip():
                clear_ids.add(part.id)
            elif (part.thought or "").strip() and owned_any.get(part.agent_id):
                kept_calls.append(part)  # over-clear the OLD set logic would have made
            pending[part.agent_id] = False  # LOAD-BEARING: tool_call always ends the step

    for part in kept_calls:  # reload KEEP audit (no silent reload keep)
        stream_audit(
            TOOL_THOUGHT_STAGE,
            agent_id=part.agent_id,
            field="next_thought",
            visible=False,
            duplicate_suppressed=False,
            duplicate_reason=REASON_RELOAD_KEEP,
            origin="message_read",
            head=(part.thought or "")[:120],
        )
    if not clear_ids:
        return message
    new_parts: list[Part] = []
    for part in parts:
        if part.id in clear_ids:
            stream_audit(
                TOOL_THOUGHT_STAGE,
                agent_id=part.agent_id,
                field="next_thought",
                visible=False,
                duplicate_suppressed=True,
                duplicate_reason=REASON_OWNS_ROW,
                origin="message_read",
                head=(part.thought or "")[:120],
            )
            new_parts.append(part.model_copy(update={"thought": ""}))
        else:
            new_parts.append(part)
    return message.model_copy(update={"parts": new_parts})
