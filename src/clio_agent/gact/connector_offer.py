"""Connector discovery: the agent offers a connector the person has not turned on.

A request can need a connector the person has but has not enabled ("show me this VTK
file in 3D" while the 3D viewer's connector is off). The agent learns which
connectors it could offer from the ``session_facts_connectors`` piece of the session
facts (:func:`offerable_facts`), and offers one with the native tool
``offer_connector(connector, reason)``. CLIO never turns a connector on by itself.

The offer is the in-conversation setup card (CN5, one way to ask): a pending
interaction of kind ``connector_setup`` with ``intent: enable`` with the connector's logo,
description, the agent's reason and its needs (sign-in rows excepted: the first call
asks for those through the same card). The call waits on its tool thread with no
timeout (plan rule 6); stopping the turn ends it.

* **Turn on** (``submit``): the values typed are saved, the connector is enabled for
  every conversation (``scope: all``) or for this conversation's agent only (CN10),
  CLIO connects and lists its tools, and its plugin joins (CN9). The connector's
  namespace is then mounted in the running turn's fleet
  (:func:`clio_agent.gact.connector_rebind.join_namespace`) and the turn's tool
  refresh takes it, so its tools are callable from the agent's next step in the
  same turn.
* **Not now** (``decline``): the typed refusal ``connector_offer_declined`` naming
  the connector (``refusal``, not ``error``: the offer was answered, the tool did not
  fail; its row reads declined). Nothing changes and nothing is retried.

The row reads "Offer <connector>"; expansion shows the agent's reason, what the
person chose and the tools that joined, or the typed refusal.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from clio_agent import conf
from clio_agent.gact.agents.tool_instrumentation import native_tool
from clio_agent.runtime import trace

TOOL_NAME = "offer_connector"
#: Typed refusal: the person answered "Not now".
OFFER_DECLINED = "connector_offer_declined"
#: Typed outcome: the turn was stopped while the card waited.
OFFER_CANCELLED = "connector_offer_cancelled"


def offer_listed() -> int:
    """How many offerable connectors the session facts name (``connectors.offer_listed``)."""
    value = conf.resolve(
        "connectors.offer_listed", env="CLIO_CONNECTORS_OFFER_LISTED", default=20, cast=conf.as_int
    )
    if value < 0:
        raise ValueError(f"connectors.offer_listed must be at least 0, not {value}")
    return value


# ------------------------------------------------------------- what is offerable


def _session(app: Any, session_id: str) -> Any:
    sessions = getattr(getattr(app, "state", None), "sessions", None)
    return sessions.get(session_id) if sessions is not None and session_id else None


def session_agent(app: Any, session_id: str) -> tuple[tuple[str, ...], str, str]:
    """``(refs, ref, name)`` of the session's agent: the refs a connector scope matches."""

    from clio_agent.gact.agents.resolution import (  # noqa: PLC0415
        AGENT_REF_KEY,
        SessionAgentUnavailable,
        session_resolution,
    )

    session = _session(app, session_id)
    ref = str(((getattr(session, "metadata", None) or {}).get(AGENT_REF_KEY)) or "")
    try:
        agent = session_resolution(app, session_id).agent if session is not None else {}
    except (SessionAgentUnavailable, RuntimeError) as exc:
        trace.event("TOOLS", "connector_offer_agent_unresolved session=%s (%s)", session_id, exc)
        agent = {}
    refs = tuple(
        dict.fromkeys(
            r for r in (ref, str(agent.get("ref") or ""), str(agent.get("extends") or "")) if r
        )
    )
    return refs, ref or str(agent.get("ref") or ""), str(agent.get("name") or ref)


def offerable(app: Any, session_id: str) -> list[Any]:
    """The connectors that do not reach this conversation's agent, by name."""

    from clio_agent.connectors import state  # noqa: PLC0415
    from clio_agent.connectors.follow import applicable  # noqa: PLC0415

    refs, _ref, _name = session_agent(app, session_id)
    if not refs:  # no agent to turn one on for (a scope names agents)
        trace.event("TOOLS", "connector_offer_no_agent session=%s", session_id)
        return []
    on = set(applicable(refs))
    return sorted(
        (found for cid, found in state.all_entries().items() if cid not in on),
        key=lambda found: found.name.casefold(),
    )


def _brief(found: Any) -> dict[str, str]:
    return {"id": found.id, "name": found.name, "description": found.description}


def offerable_facts(app: Any, session_id: str) -> tuple[list[dict[str, str]], int]:
    """The offerable connectors the session facts name (at most ``offer_listed``), and
    how many there are."""

    if not session_id:
        return [], 0
    try:
        found = offerable(app, session_id)
    except Exception as exc:  # noqa: BLE001 - facts never break a prompt; typed
        trace.event("TOOLS", "connector_offer_facts_unavailable (%s)", type(exc).__name__)
        return [], 0
    return [_brief(row) for row in found[: offer_listed()]], len(found)


def _match(entries: Mapping[str, Any], connector: str) -> Any:
    wanted = connector.strip().casefold()
    for found in entries.values():
        if wanted in {found.id.casefold(), found.name.casefold()}:
            return found
    return None


# ------------------------------------------------------------------ the card


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _card_row(app: Any, session_id: str, found: Any, reason: str) -> dict[str, Any]:
    from clio_agent.connectors import state  # noqa: PLC0415
    from clio_agent.gact import connectors_service as service  # noqa: PLC0415
    from clio_agent.tools.tool_observation import peek_call_identity  # noqa: PLC0415

    _refs, ref, name = session_agent(app, session_id)
    choice = state.choice(found.id)
    return {
        "id": uuid.uuid4().hex[:12],
        "intent": "enable",
        "session_id": session_id,
        "connector_id": found.id,
        "connector_name": found.name,
        "description": found.description,
        "reason": reason.strip(),
        "agent_ref": ref,
        "agent_name": name,
        "tool_name": TOOL_NAME,
        "invocation_id": peek_call_identity(),
        # Every need but a sign-in, which only exists once its connection does: the
        # first call of an enabled connector asks for it through the same card (CN5).
        "missing": [
            service.need_row(found, choice, need) for need in found.needs if need.kind != "sign-in"
        ],
        "asks_sign_in": any(need.kind == "sign-in" for need in found.needs),
        "status": "pending",
        "created_at": _now(),
    }


def _scope(found: Any, row: Mapping[str, Any], metadata: Mapping[str, Any]) -> Any:
    from clio_agent.connectors import state  # noqa: PLC0415
    from clio_agent.gact.connector_setup import SetupResponseError  # noqa: PLC0415

    where = str(metadata.get("scope") or "all")
    if where == "all":
        return "all"
    if where != "agent":
        raise SetupResponseError(422, "connector_scope_invalid", 'scope must be "all" or "agent"')
    ref = str(row.get("agent_ref") or "")
    if not ref:
        raise SetupResponseError(
            422, "connector_scope_invalid", "This conversation's agent has no reference"
        )
    current = state.choice(found.id)
    agents = set(current.scope.get("agents") or ()) if isinstance(current.scope, dict) else set()
    if current.enabled and current.scope == "all":
        return "all"  # already everywhere: turning it on for one agent narrows nothing
    return {"agents": sorted(agents | {ref})}


async def answer(
    app: Any,
    row: dict[str, Any],
    action: str,
    values: Mapping[str, Any],
    metadata: Mapping[str, Any],
) -> None:
    """Answer an offer card: ``submit`` turns the connector on, ``decline`` refuses."""

    from clio_agent.connectors import secrets, state  # noqa: PLC0415
    from clio_agent.gact import connectors_service as service  # noqa: PLC0415
    from clio_agent.gact.connector_setup import SetupResponseError  # noqa: PLC0415

    if action == "decline":
        row.update(status="declined", updated_at=_now())
        return
    if action != "submit":
        raise SetupResponseError(422, "validation_error", "action must be submit or decline")
    try:
        found = service.entry(row["connector_id"])
        plain, secret = service.check_values(found, values or {}, by_agent=False)
    except service.ConnectorError as exc:
        raise SetupResponseError(exc.status, exc.code, str(exc)) from None
    from clio_agent.gact.connector_values import (  # noqa: PLC0415
        default_keys,
        session_choice,
        write_overlay,
    )

    scope = _scope(found, row, metadata)
    try:
        defaults = default_keys(metadata, plain)  # CV-D5: the rest is this conversation's
    except service.ConnectorError as exc:
        raise SetupResponseError(exc.status, exc.code, str(exc)) from None
    sid = str(row["session_id"])
    if secret:
        await asyncio.to_thread(secrets.save, found.id, secret)
    if defaults:
        await asyncio.to_thread(state.update, found.id, values=defaults)
    own = {key: value for key, value in plain.items() if key not in defaults}
    if own:
        await asyncio.to_thread(write_overlay, app, sid, found.id, own)
    absent = [
        need
        for need in await asyncio.to_thread(service.missing, found, session_choice(app, sid, found))
        if need.kind != "sign-in"
    ]
    if absent:
        raise SetupResponseError(
            422,
            "connector_setup_incomplete",
            f"{found.name} still needs: {', '.join(need.label for need in absent)}",
            missing=[need.key for need in absent],
        )
    try:
        await service.enable(app, found.id, scope)
    except service.ConnectorError as exc:
        raise SetupResponseError(exc.status, exc.code, str(exc)) from None
    row.update(status="submitted", scope=scope, updated_at=_now())
    trace.event("TOOLS", "connector_offer_accepted connector=%s scope=%s", found.id, scope)


# ------------------------------------------------------- joining the running turn


def join_running_turn(app: Any, connector_id: str) -> tuple[list[str], str]:
    """Give the running turn the connector's tools; ``(tool names, typed reason)``.

    The tools are callable from the agent's next step: the namespace is mounted in the
    turn's fleet and the turn's per-step tool refresh takes it whole.
    """

    from clio_agent.gact import connectors_service as service  # noqa: PLC0415
    from clio_agent.gact.agents.clio_react import active_loop  # noqa: PLC0415
    from clio_agent.gact.agents.live_tool_refresh import LiveToolRefresher  # noqa: PLC0415
    from clio_agent.gact.connector_rebind import ConnectorJoinError, join_namespace  # noqa: PLC0415

    program = getattr(active_loop(), "agent", None)
    refresher = getattr(program, "_clio_tool_refresh", None)
    if program is None or refresher is None or not hasattr(refresher, "inner"):
        return [], "connector_join_no_turn"
    inner = refresher.inner
    executor = getattr(inner, "executor", None)
    agent = getattr(app.state, "agent", None)
    if executor is None:
        from clio_agent.gact.agents.builders import (  # noqa: PLC0415
            _active_base_agent_tool_executor,
        )

        try:
            executor = _active_base_agent_tool_executor(agent)
        except Exception as exc:  # noqa: BLE001 - typed below
            trace.event("TOOLS", "connector_join_fleet_unavailable (%s)", type(exc).__name__)
            return [], "connector_join_fleet_unavailable"
    saved = service.saved_connection(connector_id)
    if saved is None:
        return [], "connector_join_no_connection"
    from clio_agent.gact.connector_values import session_fleet, session_overlay  # noqa: PLC0415

    sid = _session_id()
    own = session_fleet(app, sid) if connector_id in session_overlay(app, sid) else None
    try:  # CV-D5: values of this conversation's own join its own fleet
        tools = join_namespace(agent, own or executor, connector_id, saved)
    except ConnectorJoinError as exc:
        trace.event("TOOLS", "%s connector=%s", exc.reason, connector_id)
        return [], exc.reason
    if inner is None:
        inner = LiveToolRefresher(executor, set())
        refresher.inner = inner
    if own is not None and own is not inner.executor:
        inner.move(connector_id, own)
    else:
        inner.take(connector_id)
    return sorted(tools), ""


# -------------------------------------------------------------------- the tool


def _session_id() -> str:
    from clio_agent.gact import context  # noqa: PLC0415

    return str(context.active_tool_session_id() or context.active_session_id() or "")


def _turned_on(app: Any, found: Any, scope: Any, already: bool) -> dict[str, Any]:
    from clio_agent.connectors import state  # noqa: PLC0415
    from clio_agent.gact import connectors_service as service  # noqa: PLC0415
    from clio_agent.tools.mcp_server_instructions import instructions_for  # noqa: PLC0415

    tools, reason = join_running_turn(app, found.id)
    row = service.row(app, found, state.choice(found.id))
    status = row["state"]
    words = {
        "ready": "ready",
        "needs": f"waiting for: {', '.join(status.get('needs') or ())}",
        "unreachable": f"not reachable: {status.get('reason') or 'no reason given'}",
    }.get(status.get("status"), str(status.get("status")))
    where = "every conversation" if scope == "all" else "this conversation's agent"
    said = (
        f"{found.name} is already on for this agent"
        if already
        else f"The person turned on {found.name} for {where}"
    )
    if tools:
        said += f"; its {len(tools)} tools are in your tool list from your next step"
    elif reason:
        said += f"; its tools could not join this turn ({reason}) and arrive with the next message"
    result: dict[str, Any] = {
        "connector": found.id,
        "connector_name": found.name,
        "enabled": True,
        "already_on": already,
        "scope": scope,
        "state": status,
        "tool_count": row.get("tool_count"),
        "tools": tools,
        "plugin": row.get("plugin"),
        "message": f"{said}. The connector is {words}.",
    }
    if reason:
        result["join_reason"] = reason
    if row.get("plugin"):
        result["message"] += (
            f" Its plugin {row['plugin']} joins this conversation; its skills are offered "
            "from the next message."
        )
    if text := instructions_for(found.id):
        result["instructions"] = text
    return result


async def offer_connector(connector: str = "", reason: str = "") -> dict[str, Any]:
    """Offer the person a connector that is off; wait for Turn on or Not now."""

    from clio_agent.connectors import state  # noqa: PLC0415
    from clio_agent.connectors.follow import applicable  # noqa: PLC0415
    from clio_agent.gact import context  # noqa: PLC0415
    from clio_agent.gact.connector_setup import wait_for_card  # noqa: PLC0415

    app = context.active_app()
    session_id = _session_id()
    if app is None or not session_id:
        return {
            "error": "connector_offer_no_conversation",
            "message": "No conversation can show the offer.",
        }
    offer = await asyncio.to_thread(offerable, app, session_id)
    briefs = [_brief(found) for found in offer]
    if not connector.strip():
        return {
            "offerable": briefs,
            "message": f"{len(briefs)} connector{'s' if len(briefs) != 1 else ''} "
            "the person can turn on.",
        }
    entries = state.all_entries()
    found = _match(entries, connector)
    if found is None:
        return {
            "error": "connector_not_found",
            "message": f"No connector {connector!r}.",
            "offerable": briefs,
        }
    refs, _ref, _name = session_agent(app, session_id)
    if found.id in applicable(refs):
        return await asyncio.to_thread(_turned_on, app, found, state.choice(found.id).scope, True)
    row = await asyncio.to_thread(_card_row, app, session_id, found, reason)
    trace.event("TOOLS", "connector_offered connector=%s session=%s", found.id, session_id)
    if await asyncio.to_thread(wait_for_card, app, row):
        return {
            "error": OFFER_CANCELLED,
            "outcome": "cancelled",
            "connector": found.id,
            "connector_name": found.name,
            "reason": reason,
            "message": f"The turn was stopped while the offer of {found.name} waited.",
        }
    if row["status"] != "submitted":
        # The offer was made and answered: "Not now" is the person's refusal, not the
        # tool failing, so it is a typed ``refusal`` the row shows as declined.
        return {
            "refusal": OFFER_DECLINED,
            "connector": found.id,
            "connector_name": found.name,
            "reason": reason,
            "message": f"The person chose not to turn on {found.name} ({found.id}) now. "
            "Nothing changed.",
        }
    result = await asyncio.to_thread(_turned_on, app, found, row.get("scope", "all"), False)
    return {**result, "reason": reason}


# --------------------------------------------------------------- presentation


def _scope_words(scope: Any) -> str:
    if scope == "all":
        return "Every conversation"
    if isinstance(scope, Mapping):
        return "Only " + ", ".join(str(ref) for ref in scope.get("agents") or ())
    return str(scope or "")


def present(args: Mapping[str, Any], result: Any, structured: Any) -> dict[str, Any]:
    """One row: Offer <connector>; the reason, then what the person chose.

    "Not now" is an answered offer, so the call keeps its success mark; the answer
    block and the card's own line ("not now") say what the person chose."""

    row = structured if isinstance(structured, Mapping) else result
    row = row if isinstance(row, Mapping) else {}
    name = str(row.get("connector_name") or args.get("connector") or "")
    # The row reads "Offer <connector>": the subject is the connector's own block.
    blocks: list[dict[str, Any]] = [
        {"id": "connector", "type": "text", "text": name or "connector"},
    ]
    if reason := str(row.get("reason") or args.get("reason") or ""):
        blocks.append({"id": "reason", "type": "text", "label": "Why", "text": reason})
    if "offerable" in row and not row.get("error"):
        listed = row.get("offerable") or []
        blocks.append(
            {
                "id": "offerable",
                "type": "text",
                "label": "Can be turned on",
                "text": "\n".join(
                    f"{item.get('name')}: {item.get('description')}" for item in listed
                )
                or "None",
            }
        )
        blocks[0] = {"id": "listed", "type": "text", "text": "connectors to offer"}
        return {
            "action": "List",
            "subject": "listed",
            "summary": str(row.get("message") or ""),
            "blocks": blocks,
        }
    error = str(row.get("error") or "")
    if row.get("refusal") == OFFER_DECLINED:
        blocks.append({"id": "outcome", "type": "text", "label": "Answer", "text": "Not now"})
        return {
            "action": "Offer",
            "subject": "connector",
            "summary": str(row.get("message") or ""),
            "blocks": blocks,
        }
    if error:
        blocks.append(
            {
                "id": "outcome",
                "type": "text",
                "label": "Not offered",
                "severity": "error",
                "text": str(row.get("message") or error),
                "detail": error,
            }
        )
        if row.get("offerable"):
            blocks.append(
                {
                    "id": "offerable",
                    "type": "text",
                    "label": "Can be turned on",
                    "text": "\n".join(str(item.get("name")) for item in row["offerable"]),
                }
            )
        return {
            "action": "Offer",
            "subject": "connector",
            "status": "cancelled" if row.get("outcome") == "cancelled" else "failed",
            "summary": str(row.get("message") or ""),
            "blocks": blocks,
        }
    blocks.append(
        {
            "id": "outcome",
            "type": "text",
            "label": "Answer",
            "text": "Already on" if row.get("already_on") else "Turned on",
        }
    )
    blocks.append(
        {"id": "scope", "type": "text", "label": "Where", "text": _scope_words(row.get("scope"))}
    )
    status = row.get("state") if isinstance(row.get("state"), Mapping) else {}
    tools = [str(name) for name in row.get("tools") or []]
    blocks.append(
        {
            "id": "tools",
            "type": "text",
            "label": f"Tools ({len(tools)})" if tools else "Tools",
            "text": "\n".join(tools) if tools else str(row.get("join_reason") or "None yet"),
            **({"severity": "warning"} if not tools else {}),
        }
    )
    if status.get("status") == "unreachable":
        blocks.append(
            {
                "id": "state",
                "type": "text",
                "label": "Can't be reached",
                "severity": "error",
                "text": str(status.get("reason") or ""),
            }
        )
    return {
        "action": "Offer",
        "subject": "connector",
        "summary": str(row.get("message") or ""),
        "blocks": blocks,
    }


def present_start(args: Mapping[str, Any]) -> dict[str, Any]:
    """While the card waits: "Offer <connector>" with the agent's reason."""

    name = str(args.get("connector") or "")
    if not name:
        return {
            "action": "List",
            "subject": "listed",
            "summary": "",
            "blocks": [{"id": "listed", "type": "text", "text": "connectors to offer"}],
        }
    blocks: list[dict[str, Any]] = [{"id": "connector", "type": "text", "text": name}]
    if reason := str(args.get("reason") or ""):
        blocks.append({"id": "reason", "type": "text", "label": "Why", "text": reason})
    return {"action": "Offer", "subject": "connector", "summary": "", "blocks": blocks}


def build_offer_connector_tool() -> Any:
    return native_tool(
        offer_connector,
        name=TOOL_NAME,
        domain="tasks",
        title="Offer connector",
        desc=(
            "Offer the person a connector they have not turned on, when their request "
            "needs what it does and none of your tools can. The person sees its logo, "
            "description and your reason, and chooses Turn on or Not now; you never turn "
            "it on yourself. On Turn on its tools are in your tool list from your next "
            "step and you continue the request; on Not now you get a refusal and nothing "
            "changed. With no connector it lists the connectors the person can turn on."
        ),
        args={
            "connector": {"type": "string", "description": "The connector's id or name."},
            "reason": {
                "type": "string",
                "description": "One sentence for the person: what it would do for this request.",
            },
        },
        presentation=present,
        presentation_start=present_start,
    )
