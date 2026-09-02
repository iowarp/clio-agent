"""How a mid-turn user steer is composed for the model and settled (#1036/#1052).

The :mod:`clio_agent.gact.loop_inbox` carrier owns *buffering and draining* wakes;
this module owns the one question the steer kind adds on top: given an accepted
steer, what does the model actually SEE, and what makes it settled?

Two invariants live here:

* **A steer is legitimate with text, with attachments, or with both.** An
  attachment-only steer ("look at this file") is real user intent, so the block
  references the attached resources through the SAME description the ordinary
  turn path uses (``resource_enrichment.describe_resource_parts``). Image parts
  cannot be folded into a turn already in flight, so they are NAMED rather than
  silently dropped.
* **Consumption is the gate, not the claim.** :func:`mark_steer_consumed` returns
  whether the intent actually settled; a caller withholds the block when it did
  not, so a steer cancelled between claim and settle is never shown to the model
  and its claim is released for a later boundary.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from fastapi import FastAPI

    from clio_agent.gact.loop_inbox import InboxEvent

logger = logging.getLogger(__name__)

# Grounding-block header for a mid-turn user steer (#1036). Its OWN marker, NOT the
# task-notification marker: a steer is USER-authored (trusted) but still rides the
# server-grounding lane (surfaced in the model's tool-observation string), never the
# model-output lane, so it is unambiguously labelled as an out-of-band user message
# that arrived while the turn was running.
USER_STEER_MARKER = "## Mid-turn user steer (a message the user sent while this turn was running)"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def steer_parts(event: "InboxEvent") -> list[Any]:
    """Coerce an event's carried steer parts into :class:`Part` objects.

    ``enqueue_user_steer`` documents ``steer_parts`` as "wire dicts or Part
    objects" (the POST route hands Parts; a rehydrated/recovered event can hand
    dicts). Every consumer reads them as Parts, so normalize once here and drop
    anything that will not coerce rather than raising into the drain.
    """

    from clio_agent.gact.types import Part  # noqa: PLC0415

    parts: list[Any] = []
    for raw in event.steer_parts or []:
        if isinstance(raw, Part):
            parts.append(raw)
            continue
        if isinstance(raw, dict):
            try:
                parts.append(Part(**raw))
            except (TypeError, ValueError) as exc:
                logger.warning(
                    "steer part skipped reason=steer_part_invalid steer_id=%s err=%r",
                    event.steer_message_id,
                    exc,
                )
    return parts


def compose_steer_block(app: "FastAPI", sid: str, event: "InboxEvent") -> str:
    """Compose the model-facing steer grounding block for one steer event.

    Returns ``""`` only when there is genuinely nothing to say (no text and no
    describable attachment) — see the module docstring for why an attachment-only
    steer is NOT that case.
    """

    from clio_agent.gact.resource_enrichment import (  # noqa: PLC0415
        ATTACHMENT_MARKER,
        ATTACHMENT_PREAMBLE,
        describe_resource_parts,
    )

    parts = steer_parts(event)
    sections: list[str] = []
    resource_lines = describe_resource_parts(app, sid, parts)
    if resource_lines:
        sections.append(
            ATTACHMENT_MARKER + "\n\n" + ATTACHMENT_PREAMBLE + "\n\n" + "\n".join(resource_lines)
        )
    image_count = sum(1 for part in parts if getattr(part, "type", "") == "image")
    if image_count:
        sections.append(
            f"- {image_count} image attachment(s) accompany this steer. Image bytes cannot be "
            "folded into a turn that is already running; they stay on the user's message and are "
            "delivered natively on the next turn that carries it."
        )
    steer_text = (event.text or "").strip()
    if steer_text:
        sections.append(steer_text)
    if not sections:
        return ""
    return USER_STEER_MARKER + "\n\n" + "\n\n".join(sections)


def mark_steer_consumed(app: "FastAPI", sid: str, event: "InboxEvent") -> bool:
    """Settle an ALREADY persisted steer at the drain and re-publish its message.

    Acceptance (``message_submission.accept_message``) durably wrote the pending
    transcript row + intent before returning its ``202``, so nothing is created
    here: we flip ``pending_steer`` off, stamp ``mid_turn_steer``/``consumed_at``
    on the SAME record, mark the intent consumed, and re-publish
    ``message.created`` so SSE clients see the steer take effect. Runs on the
    tool-executor thread (thread-safe, exactly as the drain already publishes
    ``loop_inbox.drained`` + the delegation terminals).

    Returns ``True`` only when the intent actually settled. On any failure — the
    transcript identity is gone, the row was cancelled between claim and settle —
    it logs a typed reason, RELEASES the claim so the steer is re-drivable at the
    next boundary, and returns ``False``.
    """

    try:
        from clio_agent.gact.events import Event  # noqa: PLC0415
        from clio_agent.gact.session_store import _replace_session_messages  # noqa: PLC0415

        messages = list(app.state.messages.get(sid, []))
        msg = next((row for row in messages if row.id == event.steer_message_id), None)
        if msg is None:
            raise ValueError(f"pending steer transcript missing: {event.steer_message_id}")
        if app.state.message_intents.mark_consumed(sid, event.steer_message_id) is None:
            raise ValueError(f"pending steer no longer claimed: {event.steer_message_id}")
        metadata = dict(msg.metadata)
        metadata["pending_steer"] = False
        metadata["mid_turn_steer"] = True
        metadata["consumed_at"] = _now_iso()
        settled = msg.model_copy(update={"metadata": metadata, "updated_at": _now_iso()})
        _replace_session_messages(
            app, sid, [settled if row.id == settled.id else row for row in messages]
        )
        app.state.bus.publish(
            Event(type="message.created", session_id=sid, payload=settled.to_wire())
        )
        return True
    except Exception as exc:  # noqa: BLE001 - a settle hiccup must not break the tool call
        logger.warning(
            "steer settle failed reason=steer_settle_error steer_id=%s err=%r",
            event.steer_message_id,
            exc,
        )
        app.state.message_intents.release_claim(sid, event.steer_message_id)
        return False


__all__ = [
    "USER_STEER_MARKER",
    "compose_steer_block",
    "mark_steer_consumed",
    "steer_parts",
]
