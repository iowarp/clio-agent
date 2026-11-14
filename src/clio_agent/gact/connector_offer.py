"""Connector discovery: the agent offers to add a connector to this conversation.

A request can need a connector this conversation does not have ("show me this VTK file
in 3D" while the 3D viewer's connector is off, or on but not added here). The agent
learns which connectors it could offer from the ``session_facts_plugins`` piece of the
session facts (:func:`clio_agent.gact.plugin_offer.offerable_facts`): every connector
whose plugin this conversation does not have, on or off, beside the plugins it could
take. It offers one with the native tool ``offer_plugin(plugin, reason)``
(:mod:`clio_agent.gact.plugin_offer`), whose connector case is this module
(:func:`offer_connector`). CLIO never adds a connector by itself.

The offer is the in-conversation setup card (CN5, one way to ask): a pending
interaction of kind ``connector_setup`` with ``intent: enable`` with the connector's
logo, description, the agent's reason, whether the connector is on, and its needs
(sign-in rows excepted: the first call asks for those through the same card). The
call waits on its tool thread with no timeout (plan rule 6); stopping the turn ends it.

* **Add** (``submit``): the values typed are saved, the connector is turned on if it
  was off (installed and readied: CLIO connects and lists its tools), and its plugin
  joins *this* conversation's delta (:func:`.connector_follow.add_to_conversation`,
  the plugin-change ``note`` mode). The connector's namespace is then mounted in the
  running turn's fleet (:func:`clio_agent.gact.connector_rebind.join_namespace`) and
  the turn's tool refresh takes it, so its tools are callable from the agent's next
  step in the same turn. No other conversation changes (CN10 *Amended*).
* **Not now** (``decline``): the typed refusal ``connector_offer_declined`` naming the
  connector (``refusal``, not ``error``: the offer was answered, the tool did not
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

from clio_agent.runtime import trace

#: The tool that makes the offer: ``offer_plugin``, whose connector case is this module.
TOOL_NAME = "offer_plugin"
#: Typed refusal: the person answered "Not now".
OFFER_DECLINED = "connector_offer_declined"
#: Typed outcome: the turn was stopped while the card waited.
OFFER_CANCELLED = "connector_offer_cancelled"


# ------------------------------------------------------------- what is offerable


#: What resolving the workspace fleet raises when it cannot be built now: a server set
#: that does not resolve (``ValueError``), a typed mount or preparation failure
#: (``RuntimeError``: ``PrepareError``, the launcher-cache lock; ``TimeoutError``:
#: ``NoProgressTimeout``) or the workspace folder (``OSError``). The join is best-effort:
#: the tools arrive with the next message, and the result says why (``join_reason``).
#: Anything else is a fault and raises.
FLEET_RESOLVE_ERRORS: tuple[type[Exception], ...] = (OSError, RuntimeError, ValueError)


def _session(app: Any, session_id: str) -> Any:
    sessions = getattr(getattr(app, "state", None), "sessions", None)
    return sessions.get(session_id) if sessions is not None and session_id else None


def session_agent(app: Any, session_id: str) -> tuple[tuple[str, ...], str, str]:
    """``(refs, ref, name)`` of the session's agent, as the card names it."""

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


def conversation_connectors(app: Any, session_id: str) -> dict[str, list[str]] | None:
    """The connectors this conversation has, with their plugin ids; None when its agent
    cannot be resolved (typed in the trace)."""

    from clio_agent.gact.agents.resolution import (  # noqa: PLC0415
        SessionAgentUnavailable,
        session_resolution,
    )

    if _session(app, session_id) is None:
        trace.event("TOOLS", "connector_offer_no_session session=%s", session_id)
        return None
    try:
        resolution = session_resolution(app, session_id)
    except (SessionAgentUnavailable, RuntimeError) as exc:
        trace.event("TOOLS", "connector_offer_agent_unresolved session=%s (%s)", session_id, exc)
        return None
    out: dict[str, list[str]] = {}
    for row in resolution.plugins:
        if row.get("follows"):
            out.setdefault(str(row["follows"]), []).append(str(row["id"]))
    return out


def offerable(app: Any, session_id: str) -> list[Any]:
    """The connectors whose plugin this conversation does not have, on or off, by name."""

    from clio_agent.connectors import state  # noqa: PLC0415

    here = conversation_connectors(app, session_id)
    if here is None:
        return []
    return sorted(
        (found for cid, found in state.all_entries().items() if cid not in here),
        key=lambda found: found.name.casefold(),
    )


def _brief(found: Any) -> dict[str, Any]:
    from clio_agent.connectors import state  # noqa: PLC0415

    return {
        "id": found.id,
        "name": found.name,
        "description": found.description,
        "enabled": state.choice(found.id).enabled,
    }


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
        # On already: the card only adds it to this conversation.
        "connector_enabled": choice.enabled,
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


async def answer(
    app: Any,
    row: dict[str, Any],
    action: str,
    values: Mapping[str, Any],
    metadata: Mapping[str, Any],
) -> None:
    """Answer an offer card: ``submit`` adds the connector to this conversation (turning
    it on first when it is off), ``decline`` refuses. ``metadata.save_as_default`` names
    the values that become the person's defaults; the others are this conversation's."""

    from clio_agent.connectors import secrets, state  # noqa: PLC0415
    from clio_agent.gact import connectors_service as service  # noqa: PLC0415
    from clio_agent.gact.connector_follow import (  # noqa: PLC0415
        ConnectorAddError,
        add_to_conversation,
    )
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
    enabled_now = not state.choice(found.id).enabled
    try:
        if enabled_now:
            await service.enable(app, found.id)
        elif defaults or secret:  # a conversation's own values reach only its fleet (CV-D5)
            await service.apply(app, found)
    except service.ConnectorError as exc:
        raise SetupResponseError(exc.status, exc.code, str(exc)) from None
    try:
        plugins = await asyncio.to_thread(
            add_to_conversation, app, str(row["session_id"]), found.id
        )
    except ConnectorAddError as exc:
        raise SetupResponseError(422, exc.reason, str(exc)) from None
    row.update(status="submitted", enabled_now=enabled_now, plugins=plugins, updated_at=_now())
    trace.event(
        "TOOLS",
        "connector_offer_accepted connector=%s enabled_now=%s plugins=%s",
        found.id,
        enabled_now,
        ",".join(plugins),
    )


# ------------------------------------------------------- joining the running turn


def join_running_turn(
    app: Any, connector_id: str, *, plugin_server: bool = False
) -> tuple[list[str], str]:
    """Give the running turn the connector's tools; ``(tool names, typed reason)``.

    The tools are callable from the agent's next step: the namespace is mounted in the
    turn's fleet and the turn's per-step tool refresh takes it whole. With
    ``plugin_server`` the namespace is an MCP server a plain plugin added to this
    conversation declares (:mod:`.plugin_offer`): it has no saved connection, and joins
    with the spec the conversation's server set declares.
    """

    from clio_agent.gact import connectors_service as service  # noqa: PLC0415
    from clio_agent.gact.agents.clio_react import active_loop  # noqa: PLC0415
    from clio_agent.gact.agents.live_tool_refresh import LiveToolRefresher  # noqa: PLC0415
    from clio_agent.gact.connector_rebind import (  # noqa: PLC0415
        ConnectorJoinError,
        conversation_spec,
        fleet_root_of,
        join_namespace,
    )

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
        except FLEET_RESOLVE_ERRORS as exc:
            trace.event("TOOLS", "connector_join_fleet_unavailable (%s)", type(exc).__name__)
            return [], "connector_join_fleet_unavailable"
    saved = {} if plugin_server else service.saved_connection(connector_id)
    if saved is None:
        return [], "connector_join_no_connection"
    from clio_agent.gact.connector_values import session_fleet, session_overlay  # noqa: PLC0415

    sid = _session_id()
    own = (
        session_fleet(app, sid)
        if not plugin_server and connector_id in session_overlay(app, sid)
        else None
    )
    target = own or executor  # CV-D5: values of this conversation's own join its own fleet
    # CV-N3: the spec the conversation's server set declares, as the fleet will mount it.
    declared = conversation_spec(app, sid, connector_id, fleet_root_of(agent, target))
    if plugin_server and declared is None:
        return [], "plugin_join_undeclared"
    try:
        tools = join_namespace(agent, target, connector_id, saved, spec=declared)
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


def _turned_on(
    app: Any, found: Any, *, enabled_now: bool, already: bool, plugins: list[str]
) -> dict[str, Any]:
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
    if already:
        said = f"{found.name} is already in this conversation"
    elif enabled_now:
        said = f"The person turned on {found.name} and added it to this conversation"
    else:
        said = f"The person added {found.name} to this conversation"
    if tools:
        said += f"; its {len(tools)} tools are in your tool list from your next step"
    elif reason:
        said += f"; its tools could not join this turn ({reason}) and arrive with the next message"
    result: dict[str, Any] = {
        "connector": found.id,
        "connector_name": found.name,
        "enabled": True,
        "enabled_now": enabled_now,
        "already_on": already,
        "state": status,
        "tool_count": row.get("tool_count"),
        "tools": tools,
        "plugins": plugins,
        "message": f"{said}. The connector is {words}.",
    }
    if reason:
        result["join_reason"] = reason
    skills = _plugin_skills(app, plugins)
    if skills:
        result["skills"] = skills
    if plugins and not already:
        result["message"] += f" Its plugin {', '.join(plugins)} joined this conversation only."
    if skills:  # loadable in this turn (CF-D6)
        result["message"] += (
            " Its skills load with load_skill from your next step: "
            f"{', '.join(skill['id'] for skill in skills)}."
        )
    if text := instructions_for(found.id):
        result["instructions"] = text
    return result


def _plugin_skills(app: Any, plugins: list[str]) -> list[dict[str, str]]:
    """The skills the connector's plugins bring this conversation ({id, description})."""

    from clio_agent.gact.agents.resolution import (  # noqa: PLC0415
        SessionAgentUnavailable,
        session_resolution,
    )

    sid = _session_id()
    if not plugins or not sid or _session(app, sid) is None:
        return []
    try:
        rows = session_resolution(app, sid).skills
    except (SessionAgentUnavailable, RuntimeError) as exc:
        trace.event("TOOLS", "connector_offer_skills_unavailable session=%s (%s)", sid, exc)
        return []
    return [
        {"id": str(row["id"]), "description": str(row.get("description") or "")}
        for row in rows
        if row.get("plugin") in plugins
    ]


async def offer_connector(connector: str = "", reason: str = "") -> dict[str, Any]:
    """Offer to add a connector to this conversation; wait for Add or Not now.

    ``offer_plugin``'s connector case (:mod:`clio_agent.gact.plugin_offer`).
    """

    from clio_agent.connectors import state  # noqa: PLC0415
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
            "the person can add to this conversation.",
        }
    entries = state.all_entries()
    found = _match(entries, connector)
    if found is None:
        return {
            "error": "connector_not_found",
            "message": f"No connector {connector!r}.",
            "offerable": briefs,
        }
    here = await asyncio.to_thread(conversation_connectors, app, session_id)
    present = (here or {}).get(found.id) or []
    if present and state.choice(found.id).enabled:
        return await asyncio.to_thread(
            _turned_on, app, found, enabled_now=False, already=True, plugins=present
        )
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
            "message": f"The person chose not to add {found.name} ({found.id}) now. "
            "Nothing changed.",
        }
    result = await asyncio.to_thread(
        _turned_on,
        app,
        found,
        enabled_now=bool(row.get("enabled_now")),
        already=False,
        plugins=list(row.get("plugins") or ()),
    )
    return {**result, "reason": reason}


# --------------------------------------------------------------- presentation


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
                "label": "Can be added",
                "text": "\n".join(
                    f"{item.get('name')} ({'on' if item.get('enabled') else 'off'}): "
                    f"{item.get('description')}"
                    for item in listed
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
                    "label": "Can be added",
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
            "text": "Already here"
            if row.get("already_on")
            else ("Turned on and added" if row.get("enabled_now") else "Added"),
        }
    )
    blocks.append({"id": "scope", "type": "text", "label": "Where", "text": "This conversation"})
    status = told if isinstance(told := row.get("state"), Mapping) else {}
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
    if skills := [s for s in row.get("skills") or [] if isinstance(s, Mapping)]:
        blocks.append(
            {
                "id": "skills",
                "type": "text",
                "label": f"Skills ({len(skills)})",
                "text": "\n".join(
                    f"{skill.get('id')}: {skill.get('description') or ''}".rstrip(": ")
                    for skill in skills
                ),
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
