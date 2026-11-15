"""The agent asks for optional needs of a connector that is already on (KiCad plan KC5).

A connector can work without an optional need and work better with it: the KiCad
connector searches parts on jlcsearch with no key, and Nexar, DigiKey or Mouser keys add
their catalogs. When a tool's result shows a key would help, the agent calls
``offer_plugin(plugin, reason, needs=[keys])``. CLIO raises the connector's own setup
card (kind ``connector_setup``, ``intent: "values"``) with only those rows and the
agent's reason; the person fills them or says Not now.

* **Add** is answered by :func:`clio_agent.gact.connector_offer.answer`, as an offer:
  a secret goes to the connection credential store (CN8), a value to this conversation
  or the person's defaults (CV-D5), and the connector's server restarts with them (CN7).
  The agent then calls the tool again.
* **Not now** is the typed refusal ``connector_values_declined``. The same needs are not
  asked again in this conversation: a second call returns the refusal with no card, so
  the person is not asked twice for what they declined.

Nothing here reads what a tool said: the agent decides from the result, and CLIO only
validates the keys against the connector's entry.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Sequence
from typing import Any

from clio_agent.runtime import trace

#: The card intent of a needs request.
INTENT = "values"
#: Typed refusal: the person answered Not now (now or earlier in this conversation).
DECLINED = "connector_values_declined"


def _declined(app: Any) -> dict[str, set[tuple[str, str]]]:
    held = getattr(app.state, "connector_values_declined", None)
    if held is None:
        held = {}
        app.state.connector_values_declined = held
    return held


def optional_needs(app: Any, session_id: str) -> list[dict[str, Any]]:
    """The empty optional needs of the connectors this conversation has (secrets included)."""

    from clio_agent.connectors import state  # noqa: PLC0415
    from clio_agent.gact import connectors_service as service  # noqa: PLC0415
    from clio_agent.gact.connector_offer import conversation_connectors  # noqa: PLC0415
    from clio_agent.gact.connector_values import session_choice  # noqa: PLC0415

    here = conversation_connectors(app, session_id) or {}
    entries = state.all_entries()
    out: list[dict[str, Any]] = []
    for connector_id in sorted(here):
        found = entries.get(connector_id)
        if found is None or not state.choice(found.id).enabled:
            continue
        choice = session_choice(app, session_id, found)
        for need in found.needs:
            if need.required or need.kind == "sign-in" or service.has_value(found, choice, need):
                continue
            out.append(
                {
                    "connector": found.id,
                    "key": need.key,
                    "label": need.label,
                    "kind": need.kind,
                    **({"group": need.group} if need.group else {}),
                    **({"help": need.help} if need.help else {}),
                }
            )
    return out


def _row(app: Any, session_id: str, found: Any, reason: str, keys: Sequence[str]) -> dict[str, Any]:
    from clio_agent.connectors import state  # noqa: PLC0415
    from clio_agent.gact import connectors_service as service  # noqa: PLC0415
    from clio_agent.gact.connector_offer import TOOL_NAME, _now, session_agent  # noqa: PLC0415
    from clio_agent.tools.tool_observation import peek_call_identity  # noqa: PLC0415

    _refs, ref, name = session_agent(app, session_id)
    choice = state.choice(found.id)
    return {
        "id": uuid.uuid4().hex[:12],
        "intent": INTENT,
        "session_id": session_id,
        "connector_id": found.id,
        "connector_name": found.name,
        "description": found.description,
        "reason": reason.strip(),
        "agent_ref": ref,
        "agent_name": name,
        "connector_enabled": True,
        "tool_name": TOOL_NAME,
        "invocation_id": peek_call_identity(),
        "missing": [
            service.need_row(found, choice, need) for need in found.needs if need.key in keys
        ],
        "asks_sign_in": False,
        "status": "pending",
        "created_at": _now(),
    }


async def offer_needs(found: Any, reason: str, keys: Sequence[str]) -> dict[str, Any]:
    """Ask the person for ``keys`` of ``found`` (a connector already on here); wait."""

    from clio_agent.gact import context  # noqa: PLC0415
    from clio_agent.gact.connector_offer import _session_id, offer_connector  # noqa: PLC0415
    from clio_agent.gact.connector_setup import wait_for_card  # noqa: PLC0415

    app = context.active_app()
    session_id = _session_id()
    if app is None or not session_id:
        return {
            "error": "connector_offer_no_conversation",
            "message": "No conversation can show the card.",
        }
    wanted = [str(key).strip() for key in keys if str(key).strip()]
    known = {need.key: need for need in found.needs}
    unknown = [key for key in wanted if key not in known or known[key].kind == "sign-in"]
    if unknown or not wanted:
        rows = await asyncio.to_thread(optional_needs, app, session_id)
        return {
            "error": "connector_needs_unknown",
            "connector": found.id,
            "message": f"{found.name} has no need {', '.join(unknown) or '(none named)'} to ask for.",
            "optional_needs": [row for row in rows if row["connector"] == found.id],
        }
    available = {
        row["key"]
        for row in await asyncio.to_thread(optional_needs, app, session_id)
        if row["connector"] == found.id
    }
    if not available.issuperset(wanted):
        # Not in this conversation, off, or already set: the ordinary offer handles it.
        return await offer_connector(connector=found.id, reason=reason)
    declined = _declined(app).setdefault(session_id, set())
    if all((found.id, key) in declined for key in wanted):
        return {
            "refusal": DECLINED,
            "connector": found.id,
            "connector_name": found.name,
            "needs": wanted,
            "message": f"The person already chose not to give {found.name} "
            f"{', '.join(known[key].label for key in wanted)} in this conversation.",
        }
    row = await asyncio.to_thread(_row, app, session_id, found, reason, wanted)
    trace.event("TOOLS", "connector_values_asked connector=%s keys=%s", found.id, ",".join(wanted))
    if await asyncio.to_thread(wait_for_card, app, row):
        return {
            "error": "connector_offer_cancelled",
            "outcome": "cancelled",
            "connector": found.id,
            "message": f"The turn was stopped while the card for {found.name} waited.",
        }
    labels = ", ".join(known[key].label for key in wanted)
    if row["status"] != "submitted":
        declined.update((found.id, key) for key in wanted)
        return {
            "refusal": DECLINED,
            "connector": found.id,
            "connector_name": found.name,
            "needs": wanted,
            "reason": reason,
            "message": f"The person chose not to give {found.name} {labels} now. Nothing changed; "
            "carry on with what the connector has.",
        }
    return {
        "connector": found.id,
        "connector_name": found.name,
        "needs_set": wanted,
        "reason": reason,
        "message": f"The person gave {found.name} {labels}. Its server restarted with them; "
        "call the tool again.",
    }
