"""An offer the turn does not wait for (OD-D1).

``offer_plugin`` waits for Add or Not now when the agent says the request cannot go on
without the plugin (``required``). Otherwise the card stays on screen for the person and
the tool returns at once, so the agent goes on with the work that does not need the
plugin. The result says what the plugin is for (its description and the opening of the
instructions its servers last sent) and that, while the offer waits, the agent does not
do that work some other way (a shell command, a script, another tool): the person's
answer decides how it is done.

When the person answers such a card later, the answer itself is applied as for a waited
card (Add puts the plugin or connector in this conversation; Not now changes nothing),
and the agent is told through the conversation's mail: CLIO-attributed ``attention``
mail, which reaches a running agent at its next step and wakes an idle conversation.
Its tools reach a running turn when the agent offers the plugin again (an offer of a
plugin already here joins it to the turn); the next message's agent has them anyway.

A second offer of the same plugin while its card is pending shows no second card; it
returns the pending one.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from clio_agent.runtime import trace

#: Typed outcome: the card is on screen and the turn goes on.
OFFER_SHOWN = "plugin_offer_shown"
#: Typed outcome: the same offer's card is already pending in this conversation.
OFFER_PENDING = "plugin_offer_pending"
#: Typed: the person answered a card the turn did not wait for.
OFFER_ANSWERED = "plugin_offer_answered_later"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _target(row: Mapping[str, Any]) -> str:
    return str(row.get("connector_id") or row.get("plugin_id") or "")


def pending_offer(app: Any, session_id: str, target: str) -> dict[str, Any] | None:
    """The pending offer card of ``target`` in this conversation, if one is on screen."""

    from clio_agent.gact.connector_setup import OFFER_INTENTS, _rows  # noqa: PLC0415

    for row in list(_rows(app).values()):
        if (
            row["session_id"] == session_id
            and row.get("intent") in OFFER_INTENTS
            and row["status"] == "pending"
            and _target(row) == target
        ):
            return row
    return None


def show(app: Any, row: dict[str, Any]) -> dict[str, Any]:
    """Put ``row`` on screen without waiting; the result the tool returns."""

    from clio_agent.gact.connector_setup import _publish, _rows  # noqa: PLC0415

    row["detached"] = True
    _rows(app)[row["id"]] = row
    _publish(app, "connector.setup.requested", row)
    trace.event(
        "TOOLS",
        "plugin_offer_shown target=%s session=%s",
        _target(row),
        row["session_id"],
    )
    return shown_result(row, outcome=OFFER_SHOWN)


#: How much of the offered plugin's recorded server instructions the result carries.
INSTRUCTIONS_OPENING_CHARS = 1200


def offered_instructions(row: Mapping[str, Any]) -> str:
    """The opening of the instructions the offered plugin's servers last sent, else ""."""

    from clio_agent.plugins.recorded_instructions import plugin_instructions  # noqa: PLC0415

    texts: list[str] = []
    for plugin in dict.fromkeys(str(v) for v in (row.get("plugin_id"), _target(row)) if v):
        try:
            text = plugin_instructions(plugin).strip()
        except Exception as exc:  # noqa: BLE001 - the offer stands without them
            trace.event("TOOLS", "plugin_offer_instructions_unread plugin=%s (%s)", plugin, exc)
            continue
        if text and text not in texts:
            texts.append(text)
    opening = "\n".join(texts)
    if len(opening) <= INSTRUCTIONS_OPENING_CHARS:
        return opening
    return opening[:INSTRUCTIONS_OPENING_CHARS].rsplit(" ", 1)[0] + " ..."


def shown_result(row: Mapping[str, Any], *, outcome: str) -> dict[str, Any]:
    """What the agent is told about a card on screen that the turn does not wait for."""

    name = str(row.get("connector_name") or _target(row))
    again = outcome == OFFER_PENDING
    description = str(row.get("description") or "").strip()
    instructions = offered_instructions(row)
    result: dict[str, Any] = {
        "outcome": outcome,
        "plugin": _target(row),
        "plugin_name": name,
        "reason": row.get("reason", ""),
        "offered": True,
    }
    if description:
        result["description"] = description
    if instructions:
        result["instructions"] = instructions
    said = " and ".join(
        part
        for part, present in (("description", description), ("instructions", instructions))
        if present
    )
    result["message"] = (
        f"The offer of {name} is {'already ' if again else ''}on screen for the person. "
        + (f"Its {said} here say what it is for and how that work is done. " if said else "")
        + f"While the offer waits, do not do {name}'s work some other way (a shell "
        "command, a script, another tool or a substitute you make): the person's answer "
        "decides how it is done. Go on only with the work that does not need it. If they "
        "add it, you are told, and its tools join when you offer it again."
    )
    return result


def answered_text(row: Mapping[str, Any]) -> str:
    """The note the agent gets when the person answers a card it did not wait for."""

    name = str(row.get("connector_name") or _target(row))
    if row.get("status") == "submitted":
        return (
            f"The person added {name} ({_target(row)}), which you offered earlier in this "
            f"conversation (your reason: {row.get('reason') or 'none given'}). Its skills "
            f"and instructions are in this conversation now; to use its tools in this turn, "
            f"call offer_plugin with plugin {_target(row)!r} again, which joins them. "
            "Use it where it improves what you have done or are doing."
        )
    return (
        f"The person chose not to add {name} ({_target(row)}), which you offered earlier "
        "in this conversation. Nothing changed."
    )


def tell_agent(app: Any, row: Mapping[str, Any]) -> None:
    """Send the answer of a card the turn did not wait for to the conversation's mail."""

    from clio_agent.arc.task_types import MailItem  # noqa: PLC0415

    history = getattr(app.state, "task_history", None)
    session_id = str(row["session_id"])
    if history is None:
        trace.event(
            "TOOLS",
            "%s undelivered reason=no_task_history target=%s session=%s",
            OFFER_ANSWERED,
            _target(row),
            session_id,
        )
        return
    with history.serialized():
        content = history.write_content(session_id, answered_text(row))
        history.record_mail(
            MailItem(
                id="mail_" + uuid4().hex,
                recipient_session=session_id,
                kind="attention",
                content=content,
                created_at=_now(),
            )
        )
    trace.event(
        "TOOLS",
        "%s status=%s target=%s session=%s",
        OFFER_ANSWERED,
        row.get("status"),
        _target(row),
        session_id,
    )
