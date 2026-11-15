"""Plugin discovery: the agent offers to add a plugin to this conversation (CF-D3 to CF-D7).

A request can need what a plugin the person has installed brings (its skills, tools or
servers) while this conversation does not carry it. The agent learns which plugins it
could offer from the ``session_facts_plugins`` piece of the session facts
(:func:`offerable_facts`): every plugin of the person's library this conversation does
not have, and every connector whose plugin it does not have, on or off, each as
``name (id): one-line summary``, capped (``plugins.offer_listed``) with the total, and
with the person's choice over that list (:mod:`clio_agent.gact.offer_list`). It
offers one with the native tool ``offer_plugin(plugin, reason)``. The model decides
whether a request needs one; CLIO never adds a plugin by itself.

* A **connector** (or a plugin that follows one) is the connector offer of
  :mod:`clio_agent.gact.connector_offer`: its card also turns the connector on and asks
  for its needs.
* Any **other plugin** gets the same card (a ``connector_setup`` interaction with
  ``intent: add_plugin``): its name, the agent's one-line reason, Add and Not now.
  Add puts it in this conversation's delta (the plugin-change ``note`` mode, as the
  plugin picker does); its MCP servers join the running turn's fleet with the spec the
  conversation's server set declares, and its skills load with ``load_skill`` from the
  agent's next step (:mod:`clio_agent.gact.agents.skill_join`). Its Python tools arrive
  with the next message (typed ``plugin_tools_next_message``). Not now is the typed
  refusal ``plugin_offer_declined``; a conversation that cannot take the plugin answers
  the card with its typed resolution reason.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone
from typing import Any

from clio_agent import conf
from clio_agent.gact.agents.tool_instrumentation import native_tool
from clio_agent.runtime import trace

TOOL_NAME = "offer_plugin"
#: Typed refusal: the person answered "Not now".
OFFER_DECLINED = "plugin_offer_declined"
#: Typed outcome: the turn was stopped while the card waited.
OFFER_CANCELLED = "plugin_offer_cancelled"
#: The card's intent for a plugin that does not follow a connector.
INTENT = "add_plugin"
#: Typed: the plugin's Python tools are built with the next message's agent.
TOOLS_NEXT_MESSAGE = "plugin_tools_next_message"
#: How long a summary in the list is (characters).
SUMMARY_CHARS = 100


def offer_listed() -> int:
    """How many offerable plugins the session facts name (``plugins.offer_listed``)."""
    value = conf.resolve(
        "plugins.offer_listed", env="CLIO_PLUGINS_OFFER_LISTED", default=20, cast=conf.as_int
    )
    if value < 0:
        raise ValueError(f"plugins.offer_listed must be at least 0, not {value}")
    return value


def summary(text: str) -> str:
    """A description's first line, at most :data:`SUMMARY_CHARS` characters."""

    line = " ".join(str(text or "").split())
    if len(line) <= SUMMARY_CHARS:
        return line
    cut = line[: SUMMARY_CHARS - 1].rsplit(" ", 1)[0].rstrip(",;:")
    return f"{cut}…"


# ------------------------------------------------------------- what is offerable


def _ref(record: Any) -> str:
    return f"{record.marketplace}/{record.id}"


def library(app: Any) -> list[Any]:
    """The plugins a conversation can take from the person's library.

    The plugins of the marketplaces the person has added (``PluginStore.marketplaces()``:
    whichever they are, the default one included; a removed marketplace's plugins are
    gone), valid and of ``kind: plugin``; not CLIO's built-ins (an agent carries those by
    its definition), not the plugins that follow a connector (the connector stands for
    them), and trusted unless they carry nothing executable.
    """

    from clio_agent.gact.agents.resolution import plugin_service  # noqa: PLC0415
    from clio_agent.plugins.store import BUILTIN_MARKETPLACE  # noqa: PLC0415

    store = plugin_service(app).store
    added = set(store.marketplaces()) - {BUILTIN_MARKETPLACE}
    return [
        record
        for record in store.records()
        if record.kind == "plugin"
        and record.valid
        and record.marketplace in added
        and not record.follows
        and (not record.has_executable_parts or store.trusted(record))
    ]


def _here(app: Any, session_id: str) -> set[str] | None:
    from clio_agent.gact.agents.resolution import (  # noqa: PLC0415
        SessionAgentUnavailable,
        session_resolution,
    )

    try:
        return {str(row["id"]) for row in session_resolution(app, session_id).plugins}
    except (SessionAgentUnavailable, RuntimeError) as exc:
        trace.event("TOOLS", "plugin_offer_agent_unresolved session=%s (%s)", session_id, exc)
        return None


def offerable(app: Any, session_id: str) -> list[dict[str, Any]]:
    """Every plugin and connector this conversation could take, by name.

    A connector row reads ``{kind: connector, id, name, summary, enabled}``; a plugin
    row ``{kind: plugin, id, ref, name, marketplace, summary}``.
    """

    from clio_agent.gact.connector_offer import offerable as connectors  # noqa: PLC0415

    here = _here(app, session_id)
    if here is None:
        return []
    return _ordered(app, connectors(app, session_id), here)


def default_offerable(app: Any) -> list[dict[str, Any]]:
    """What a new conversation with the person's default agent could take, in order.

    The default agent is resolved without pinning (no session is touched). Raises
    :class:`~clio_agent.gact.agents.resolution.SessionAgentUnavailable` (typed) when it
    does not resolve.
    """

    from clio_agent.gact.agents.resolution import (  # noqa: PLC0415
        SessionAgentUnavailable,
        default_agent_ref,
        plugin_service,
    )
    from clio_agent.gact.connector_offer import followed_by, offerable_beside  # noqa: PLC0415
    from clio_agent.plugins.resolve import ResolutionError  # noqa: PLC0415
    from clio_agent.plugins.service import AgentNotFound  # noqa: PLC0415

    ref = default_agent_ref(app)
    try:
        resolution = plugin_service(app).resolve(ref, pin=False)
    except AgentNotFound as exc:
        raise SessionAgentUnavailable(ref, exc.reason, [{"code": exc.reason}]) from exc
    except ResolutionError as exc:
        diagnostics = [row.to_dict() for row in exc.diagnostics]
        raise SessionAgentUnavailable(ref, "agent_resolution_failed", diagnostics) from exc
    here = {str(row["id"]) for row in resolution.plugins}
    return _ordered(app, offerable_beside(followed_by(resolution.plugins)), here)


def _ordered(app: Any, found_connectors: list[Any], here: set[str]) -> list[dict[str, Any]]:
    """The rows for ``found_connectors`` and the library plugins not in ``here``, ordered
    as the session facts list them."""

    from clio_agent.connectors import state  # noqa: PLC0415

    rows: list[dict[str, Any]] = [
        {
            "kind": "connector",
            "id": found.id,
            "name": found.name,
            "summary": summary(found.description),
            "enabled": state.choice(found.id).enabled,
            "kit": bool(found.kit),
            # Discover's group: what offer_plugin searches by category (OL-D11).
            "category": found.group or "",
        }
        for found in found_connectors
    ]
    # Grouped by marketplace, the most recently added first: a marketplace the person
    # just added is not hidden behind the cap by an older, larger one (CF-D4).
    from clio_agent.gact.agents.resolution import plugin_service  # noqa: PLC0415

    added = plugin_service(app).store.marketplaces()
    order = {
        name: rank
        for rank, name in enumerate(
            sorted(
                added, key=lambda n: str((added[n] or {}).get("installed_at") or ""), reverse=True
            )
        )
    }
    plugins = [
        {
            "kind": "plugin",
            "id": record.id,
            "ref": _ref(record),
            "name": record.id,
            "marketplace": record.marketplace,
            "summary": summary(record.description),
            "category": record.category,
            "keywords": list(record.keywords),
        }
        for record in library(app)
        if record.id not in here
    ]
    plugins.sort(
        key=lambda row: (order.get(row["marketplace"], len(order)), row["name"].casefold())
    )
    # Under the cap (LZ-D10): the connectors that are on, then the catalog's own entries
    # and the clio-kit servers a plugin of the person's library brings with skills, then
    # the plugins (CF-D4's order), then the remaining clio-kit servers, which are many.
    followed = _followed(app)
    first = [row for row in rows if row["enabled"] or not row["kit"] or row["id"] in followed]
    rest = [row for row in rows if row not in first]
    first.sort(key=lambda row: (not row["enabled"], row["name"].casefold()))
    rest.sort(key=lambda row: row["name"].casefold())
    return first + plugins + rest


def _followed(app: Any) -> set[str]:
    """The connectors an installed plugin that brings skills follows."""

    from clio_agent.gact.agents.resolution import plugin_service  # noqa: PLC0415

    return {
        record.follows
        for record in plugin_service(app).store.records()
        if record.follows and record.skills
    }


def offerable_facts(app: Any, session_id: str) -> tuple[list[dict[str, Any]], int, int]:
    """The rows the session facts name, how many could be added, and how many of those
    the person left out in Settings.

    The first ``offer_listed`` rows, with the person's choice over them
    (:mod:`clio_agent.gact.offer_list`): unticked ones left out, ones ticked beyond the
    cap added, in order. The total counts every candidate (left-out ones too): the facts
    say how many more there are and that ``offer_plugin`` finds them (OL-D11).
    """

    # No session store (an app built without one) means no conversation to offer to.
    if not session_id or getattr(getattr(app, "state", None), "sessions", None) is None:
        return [], 0, 0
    from clio_agent.connectors.state import ConnectorStateError  # noqa: PLC0415
    from clio_agent.plugins.store import StoreError  # noqa: PLC0415

    try:
        rows = offerable(app, session_id)
    except (StoreError, ConnectorStateError) as exc:  # unreadable stores: no facts this step
        trace.event("TOOLS", "plugin_offer_facts_unavailable (%s)", type(exc).__name__)
        return [], 0, 0
    from clio_agent.gact.offer_list import listed, not_excluded, overlay_or_default  # noqa: PLC0415

    chosen = overlay_or_default()
    return (
        listed(rows, offer_listed(), chosen),
        len(rows),
        len(rows) - len(not_excluded(rows, chosen)),
    )


def offerable_matching(app: Any, session_id: str, query: str) -> list[dict[str, Any]]:
    """The plugins and connectors this conversation could add whose id or name a tool
    search names: a search only reaches this conversation's own tools, so a miss on
    "paraview" would otherwise read as "there is no ParaView"."""

    if not session_id or getattr(getattr(app, "state", None), "sessions", None) is None:
        return []
    from clio_agent.connectors.state import ConnectorStateError  # noqa: PLC0415
    from clio_agent.plugins.store import StoreError  # noqa: PLC0415

    try:
        rows = offerable(app, session_id)
    except (StoreError, ConnectorStateError) as exc:  # unreadable stores: no note this search
        trace.event("TOOLS", "plugin_offer_search_unavailable (%s)", type(exc).__name__)
        return []
    asked = query.casefold()
    return [row for row in rows if row["id"].casefold() in asked or row["name"].casefold() in asked]


def search_note(rows: list[dict[str, Any]]) -> str:
    """What a tool search found to add rather than to call; a row the person left out in
    Settings says so (OL-D11)."""

    from clio_agent.gact.offer_list import entry_key, overlay_or_default  # noqa: PLC0415

    left_out = overlay_or_default().exclude

    def named(row: dict[str, Any]) -> str:
        mark = ", left out by the person in Settings" if entry_key(row) in left_out else ""
        return f'{row["name"]} ({row["kind"]}, id "{row["id"]}"{mark})'

    return (
        f"Not in this conversation, but it can be added: {'; '.join(named(r) for r in rows)}. "
        "offer_plugin asks the person to add it; its tools join this conversation once they "
        "accept."
    )


def _match(app: Any, wanted: str) -> tuple[str, Any]:
    """``("connector", entry)``, ``("plugin", record)`` or ``("", None)`` for ``wanted``."""

    from clio_agent.connectors import state  # noqa: PLC0415
    from clio_agent.gact.agents.resolution import plugin_service  # noqa: PLC0415

    key = wanted.strip().casefold()
    entries = state.all_entries()
    for found in entries.values():
        if key in {found.id.casefold(), found.name.casefold()}:
            return "connector", found
    for record in plugin_service(app).store.records():
        if key in {record.id.casefold(), _ref(record).casefold()}:
            if record.follows and record.follows in entries:
                return "connector", entries[record.follows]
            if record in library(app):
                return "plugin", record
    return "", None


# ------------------------------------------------------------------ the card


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _card_row(app: Any, session_id: str, record: Any, reason: str) -> dict[str, Any]:
    from clio_agent.gact.connector_offer import session_agent  # noqa: PLC0415
    from clio_agent.tools.tool_observation import peek_call_identity  # noqa: PLC0415

    _refs, ref, agent_name = session_agent(app, session_id)
    return {
        "id": uuid.uuid4().hex[:12],
        "intent": INTENT,
        "session_id": session_id,
        # The card's common fields name the plugin; there is no connector behind it.
        "connector_id": "",
        "connector_name": record.id,
        "plugin_id": record.id,
        "plugin_ref": _ref(record),
        "description": summary(record.description),
        "reason": reason.strip(),
        "agent_ref": ref,
        "agent_name": agent_name,
        "tool_name": TOOL_NAME,
        "invocation_id": peek_call_identity(),
        "missing": [],
        "status": "pending",
        "created_at": _now(),
    }


async def answer(app: Any, row: dict[str, Any], action: str) -> None:
    """Answer a plugin offer: ``submit`` adds it to this conversation, ``decline`` refuses."""

    from clio_agent.gact.connector_follow import ConnectorAddError, add_plugins  # noqa: PLC0415
    from clio_agent.gact.connector_setup import SetupResponseError  # noqa: PLC0415

    if action == "decline":
        row.update(status="declined", updated_at=_now())
        return
    if action != "submit":
        raise SetupResponseError(422, "validation_error", "action must be submit or decline")
    try:
        resolution = await asyncio.to_thread(
            add_plugins, app, str(row["session_id"]), [str(row["plugin_ref"])]
        )
    except ConnectorAddError as exc:
        raise SetupResponseError(422, exc.reason, str(exc)) from None
    added = [str(p["id"]) for p in resolution.plugins if p["id"] == row["plugin_id"]]
    row.update(status="submitted", plugins=added, updated_at=_now())
    trace.event(
        "TOOLS",
        "plugin_offer_accepted plugin=%s session=%s",
        row["plugin_ref"],
        row["session_id"],
    )


def _joined(app: Any, session_id: str, record: Any, *, already: bool) -> dict[str, Any]:
    """What the plugin brings to the running turn, and the message that says so."""

    from clio_agent.gact.agents.resolution import session_resolution  # noqa: PLC0415
    from clio_agent.gact.connector_offer import join_running_turn  # noqa: PLC0415

    servers = sorted(
        name
        for name, row in session_resolution(app, session_id).mcp_servers.items()
        if row.get("plugin") == record.id
    )
    tools: list[str] = []
    reasons: list[str] = []
    for namespace in servers:
        joined, reason = join_running_turn(app, namespace, plugin_server=True)
        tools += joined
        if reason:
            reasons.append(reason)
    skills = [{"id": skill.id, "description": skill.description} for skill in record.skills]
    later = record.tool_names()
    said = (
        f"{record.id} is already in this conversation"
        if already
        else f"The person added the plugin {record.id} to this conversation"
    )
    parts = [said]
    if skills:
        parts.append(
            f"its skills load with load_skill from your next step: "
            f"{', '.join(skill['id'] for skill in skills)}"
        )
    if tools:
        parts.append(f"its {len(tools)} tools are in your tool list from your next step")
    if reasons:
        parts.append(f"its servers could not join this turn ({', '.join(reasons)})")
    if later:
        parts.append(f"its tools {', '.join(later)} arrive with the next message")
    result: dict[str, Any] = {
        "plugin": record.id,
        "plugin_ref": _ref(record),
        "plugin_name": record.id,
        "description": summary(record.description),
        "already_here": already,
        "skills": skills,
        "tools": sorted(tools),
        "message": "; ".join(parts) + ".",
    }
    if reasons:
        result["join_reason"] = reasons[0]
    if later:
        result["tools_next_message"] = later
        result.setdefault("join_reason", TOOLS_NEXT_MESSAGE)
    return result


async def _offer_plugin(app: Any, session_id: str, record: Any, reason: str) -> dict[str, Any]:
    from clio_agent.gact.connector_setup import wait_for_card  # noqa: PLC0415

    here = await asyncio.to_thread(_here, app, session_id)
    if here is not None and record.id in here:
        return await asyncio.to_thread(_joined, app, session_id, record, already=True)
    row = await asyncio.to_thread(_card_row, app, session_id, record, reason)
    trace.event("TOOLS", "plugin_offered plugin=%s session=%s", _ref(record), session_id)
    named = {
        "plugin": record.id,
        "plugin_name": record.id,
        "description": summary(record.description),
        "reason": reason,
    }
    if await asyncio.to_thread(wait_for_card, app, row):
        return {
            "error": OFFER_CANCELLED,
            "outcome": "cancelled",
            **named,
            "message": f"The turn was stopped while the offer of {record.id} waited.",
        }
    if row["status"] != "submitted":
        return {
            "refusal": OFFER_DECLINED,
            **named,
            "message": f"The person chose not to add the plugin {record.id} now. Nothing changed.",
        }
    result = await asyncio.to_thread(_joined, app, session_id, record, already=False)
    return {**result, "reason": reason}


# -------------------------------------------------------------------- the tool


async def offer_plugin(
    reason: str,
    plugin: str = "",
    search: str = "",
    category: str = "",
    needs: list[str] | None = None,
) -> dict[str, Any]:
    """Offer to add a plugin or connector to this conversation; wait for Add or Not now.
    With ``search`` or ``category`` instead, find what could be added (OL-D11). With
    ``needs`` and a connector already here, ask the person for those optional needs.

    ``reason`` is required (schema: no default, at least one character): the card shows
    the person why the agent asks, so an offer without one is a validation error the
    model corrects, never a card with no reason (CF-N4).
    """

    from clio_agent.gact import connector_offer, context  # noqa: PLC0415

    app = context.active_app()
    session_id = connector_offer._session_id()
    if app is None or not session_id:
        return {
            "error": "plugin_offer_no_conversation",
            "message": "No conversation can show the offer.",
        }
    if plugin.strip():  # named: offered even when left out of the list (OL-D3)
        kind, found = await asyncio.to_thread(_match, app, plugin)
        if kind == "connector" and needs:
            from clio_agent.gact.connector_needs_offer import offer_needs  # noqa: PLC0415

            return await offer_needs(found, reason, needs)
        if kind == "connector":
            return await connector_offer.offer_connector(connector=found.id, reason=reason)
        if kind == "plugin":
            return await _offer_plugin(app, session_id, found, reason)
    from clio_agent.gact import offer_search  # noqa: PLC0415
    from clio_agent.gact.offer_list import listed, overlay_or_default  # noqa: PLC0415

    chosen = await asyncio.to_thread(overlay_or_default)
    rows = await asyncio.to_thread(offerable, app, session_id)
    shown = listed(rows, offer_listed(), chosen)
    if plugin.strip():
        return {
            "error": "plugin_not_found",
            "message": f"No plugin or connector {plugin!r} the person has; the closest by "
            "its words follow, if any.",
            **offer_search.search_result(
                rows, shown, set(chosen.exclude), offer_listed(), search=plugin
            ),
        }
    if search.strip() or category.strip():
        return offer_search.search_result(
            rows, shown, set(chosen.exclude), offer_listed(), search=search, category=category
        )
    from clio_agent.gact.connector_needs_offer import optional_needs  # noqa: PLC0415

    return {
        **offer_search.overview(rows, len(shown), set(chosen.exclude)),
        # The connectors already here and their empty optional needs (such as API keys),
        # which ``needs`` can ask the person for.
        "optional_needs": await asyncio.to_thread(optional_needs, app, session_id),
    }


def build_offer_plugin_tool() -> Any:
    from clio_agent.gact.plugin_offer_presentation import present, present_start  # noqa: PLC0415

    return native_tool(
        offer_plugin,
        name=TOOL_NAME,
        domain="tasks",
        title="Offer plugin",
        desc=(
            "Offer to add one of the plugins or connectors the person has that this "
            "conversation does not, when the person's request needs what it brings. The "
            "person sees its name and your reason and chooses Add or Not now; you never add "
            "it yourself. A connector's card also turns it on and asks for what it needs. On "
            "Add its tools and skills are yours from your next step and you continue the "
            "request; on Not now you get a refusal and nothing changed. The session facts "
            "list some of them; search (words) or category finds the others, each marked "
            "listed or not and whether the person left it out in Settings. With nothing "
            "named it gives the categories and counts, and the optional needs (such as API "
            "keys) of the connectors already here. With needs it asks the person for those of "
            "a connector already here, when a tool's result shows they would help."
        ),
        args={
            "plugin": {
                "type": "string",
                "description": "The id or name of the plugin or connector to offer.",
            },
            "search": {
                "type": "string",
                "description": (
                    "Words to find plugins and connectors by (all must appear in the id, "
                    "name, summary, category or keywords). Finds; does not offer."
                ),
            },
            "category": {
                "type": "string",
                "description": "A category the overview names, to list what it holds.",
            },
            "reason": {
                "type": "string",
                "minLength": 1,
                "description": (
                    "Required. One sentence for the person, shown on the card: why this "
                    "request needs it and what it would do here. For a search, what you are "
                    "looking for."
                ),
            },
            "needs": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Optional. Keys of a connector's optional needs to ask for (from "
                    "optional_needs), for a connector already in this conversation."
                ),
            },
        },
        presentation=present,
        presentation_start=present_start,
    )
