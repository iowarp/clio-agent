"""Plugin discovery: the agent offers to add a plugin to this conversation (CF-D3 to CF-D7).

A request can need what a plugin the person has installed brings (its skills, tools or
servers) while this conversation does not carry it. The agent learns which plugins it
could offer from the ``session_facts_plugins`` piece of the session facts
(:func:`offerable_facts`): every plugin of the person's library this conversation does
not have, and every connector whose plugin it does not have, on or off, each as
``name (id): one-line summary``, capped (``plugins.offer_listed``) with the total, and
with the person's choice over that list (:mod:`clio_agent.gact.offer_list`). It
offers one with the native tool ``offer_plugin(plugin, reason)``. The model decides
whether a request needs one; CLIO adds a plugin by itself only to a new conversation,
when the person has set it up (:mod:`clio_agent.gact.plugin_join`).

* A **connector** (or a plugin that follows one) is the connector offer of
  :mod:`clio_agent.gact.connector_offer`: its card also turns the connector on and asks
  for its needs.
* Any **other plugin** gets the same card (a ``connector_setup`` interaction with
  ``intent: add_plugin``): its name, the agent's one-line reason, Add and Not now.
  Add puts it in this conversation's delta (the plugin-change ``note`` mode, as the
  plugin picker does); its MCP servers join the running turn's fleet with the spec the
  conversation's server set declares, and its skills load with ``load_skill`` from the
  agent's next step (:mod:`clio_agent.gact.agents.skill_join`). Its own tools join the
  running turn as the card's ``apply`` choice says (now, compact or note;
  :mod:`clio_agent.gact.agents.turn_rebuild`); only when that join cannot run do they
  arrive with the next message (typed ``plugin_tools_next_message``). Not now is the typed
  refusal ``plugin_offer_declined``; a conversation that cannot take the plugin answers
  the card with its typed resolution reason.
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
    from clio_agent.gact.agents.resolution import plugin_service  # noqa: PLC0415

    store = plugin_service(app).store
    # A connector stands for the plugins that follow it, so a search finds it by their words.
    followers: dict[str, list[Any]] = {}
    for record in store.records():
        if record.follows and record.valid:
            followers.setdefault(record.follows, []).append(record)
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
            # Searched, never shown: the whole descriptions and the followers' keywords.
            "about": " ".join(
                [found.description, *(r.description for r in followers.get(found.id, ()))]
            ),
            "keywords": [k for r in followers.get(found.id, ()) for k in r.keywords],
        }
        for found in found_connectors
    ]
    # Grouped by marketplace, the most recently added first: a marketplace the person
    # just added is not hidden behind the cap by an older, larger one (CF-D4).
    added = store.marketplaces()
    order = {
        name: rank
        for rank, name in enumerate(
            sorted(
                added, key=lambda n: str((added[n] or {}).get("installed_at") or ""), reverse=True
            )
        )
    }
    from clio_agent.plugins.recorded_instructions import plugin_instructions  # noqa: PLC0415

    plugins = [
        {
            "kind": "plugin",
            "id": record.id,
            "ref": _ref(record),
            "name": record.id,
            "marketplace": record.marketplace,
            "summary": summary(record.description),
            "about": record.description,
            "category": record.category,
            "keywords": list(record.keywords),
            # What its servers last said they reach: a search finds the plugin by it.
            "instructions": plugin_instructions(record.id),
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


def offerable_facts(
    app: Any, session_id: str
) -> tuple[list[dict[str, Any]], int, int, list[dict[str, Any]]]:
    """The rows the session facts name, how many could be added, how many of those the
    person left out in Settings, and the categories of the ones not named.

    The first ``offer_listed`` rows, with the person's choice over them
    (:mod:`clio_agent.gact.offer_list`): unticked ones left out, ones ticked beyond the
    cap added, in order. The total counts every candidate (left-out ones too): the facts
    say how many more there are and that ``offer_plugin`` finds them (OL-D11). The
    categories of those (with counts, uncategorized last, and the names of the ones the
    person turned on or installed and did not leave out) say what a search would reach, at
    a few words per entry, however large the catalog (OL-F4, OL-D17).
    """

    # No session store (an app built without one) means no conversation to offer to.
    if not session_id or getattr(getattr(app, "state", None), "sessions", None) is None:
        return [], 0, 0, []
    from clio_agent.connectors.state import ConnectorStateError  # noqa: PLC0415
    from clio_agent.plugins.store import StoreError  # noqa: PLC0415

    try:
        rows = offerable(app, session_id)
    except (StoreError, ConnectorStateError) as exc:  # unreadable stores: no facts this step
        trace.event("TOOLS", "plugin_offer_facts_unavailable (%s)", type(exc).__name__)
        return [], 0, 0, []
    from clio_agent.gact.offer_list import (  # noqa: PLC0415
        entry_key,
        listed,
        not_excluded,
        overlay_or_default,
    )
    from clio_agent.gact.offer_search import unlisted  # noqa: PLC0415

    chosen = overlay_or_default()
    shown = listed(rows, offer_listed(), chosen)
    named = {entry_key(row) for row in shown}
    return (
        shown,
        len(rows),
        len(rows) - len(not_excluded(rows, chosen)),
        unlisted([row for row in rows if entry_key(row) not in named], set(chosen.exclude)),
    )


def offerable_matching(app: Any, session_id: str, query: str) -> list[dict[str, Any]]:
    """The plugins and connectors this conversation could add that a tool search names:
    by id or name, or as ``offer_plugin``'s search finds them (description, keywords, the
    plugins a connector stands for, its servers' instructions), at most
    :func:`offer_listed`. A search only reaches this conversation's own tools, so a miss on
    "prepare spack" or "field notebook" (or on a site a plugin's server says it reaches)
    would otherwise read as "there is none"."""

    if not session_id or getattr(getattr(app, "state", None), "sessions", None) is None:
        return []
    from clio_agent.connectors.state import ConnectorStateError  # noqa: PLC0415
    from clio_agent.gact.offer_search import matching, words_of  # noqa: PLC0415
    from clio_agent.plugins.store import StoreError  # noqa: PLC0415

    try:
        rows = offerable(app, session_id)
    except (StoreError, ConnectorStateError) as exc:  # unreadable stores: no note this search
        trace.event("TOOLS", "plugin_offer_search_unavailable (%s)", type(exc).__name__)
        return []
    asked = query.casefold()
    named = [
        row for row in rows if row["id"].casefold() in asked or row["name"].casefold() in asked
    ]
    found = named + [row for row in matching(rows, words_of(query)) if row not in named]
    return found[: offer_listed()]


def search_note(rows: list[dict[str, Any]]) -> str:
    """What a tool search found to add rather than to call; a row the person left out of
    the list in Settings says so, as a fact that is no reason not to offer it (OL-D11,
    OL-F6)."""

    from clio_agent.gact.offer_list import (  # noqa: PLC0415
        SETTINGS_PLACE,
        entry_key,
        overlay_or_default,
    )

    left_out = overlay_or_default().exclude
    mark = f", not in your plugin list (the person's choice in {SETTINGS_PLACE})"

    def named(row: dict[str, Any]) -> str:
        note = mark if entry_key(row) in left_out else ""
        return f'{row["name"]} ({row["kind"]}, id "{row["id"]}"{note})'

    left = any(entry_key(row) in left_out for row in rows)
    return (
        f"Not in this conversation, but it can be added: {'; '.join(named(r) for r in rows)}. "
        "offer_plugin asks the person to add it; its tools join this conversation once they "
        "accept" + ("; it offers one not in your plugin list the same way." if left else ".")
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


def _joined(
    app: Any, session_id: str, record: Any, *, already: bool, turn: dict[str, Any] | None = None
) -> dict[str, Any]:
    """What the plugin brings to the running turn, and the message that says so.

    ``turn`` is the running turn's join (:func:`.agents.turn_rebuild.join_running_turn`):
    when it ran, the plugin's own tools are in this turn, not the next message.
    """

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
    joined_turn = bool(turn and (turn.get("joined") or turn.get("pending")))
    if tools and not joined_turn:  # else the join's own sentence says where they are
        parts.append(f"its {len(tools)} tools are in your tool list from your next step")
    if reasons:
        parts.append(f"its servers could not join this turn ({', '.join(reasons)})")
    if later and not joined_turn:
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
    if later and not joined_turn:
        result["tools_next_message"] = later
        result.setdefault("join_reason", TOOLS_NEXT_MESSAGE)
    if turn:
        from clio_agent.gact.agents.turn_rebuild import joined_message  # noqa: PLC0415

        result["apply"] = turn.get("apply")
        result["tools"] = sorted({*result["tools"], *(turn.get("joined") or ())})
        if said := joined_message(turn):
            result["message"] += " " + said
    return result


def _here_matching(app: Any, session_id: str, search: str) -> list[dict[str, Any]]:
    """The plugins already in this conversation that ``search`` finds as it finds the
    person's others (:func:`offer_search.found_by`): by id, description, declared
    keywords, server name or the instructions its servers last sent. A search for what
    is already here must not read as "there is none" (the agent would then work around
    it)."""

    from clio_agent.gact.agents.resolution import (  # noqa: PLC0415
        SessionAgentUnavailable,
        session_resolution,
    )
    from clio_agent.gact.offer_search import found_by, words_of  # noqa: PLC0415
    from clio_agent.plugins.recorded_instructions import plugin_instructions  # noqa: PLC0415

    words = words_of(search)
    if not words:
        return []
    try:
        resolution = session_resolution(app, session_id)
    except (SessionAgentUnavailable, RuntimeError):
        return []
    try:
        keywords = {record.id: list(record.keywords) for record in library(app)}
    except Exception:  # noqa: BLE001 - the library is a hint here, never a failure
        keywords = {}
    out: list[dict[str, Any]] = []
    for plugin in resolution.plugins:
        pid = str(plugin.get("id") or "")
        if not pid or (plugin.get("provenance") or {}).get("source") == "core":
            continue  # always here, and named in the prompt already
        servers = sorted(
            name for name, row in resolution.mcp_servers.items() if row.get("plugin") == pid
        )
        row = {
            "id": pid,
            "about": " ".join([str(plugin.get("description") or ""), *servers]),
            "keywords": keywords.get(pid) or list(plugin.get("keywords") or ()),
            "instructions": plugin_instructions(pid),
        }
        if found_by(row, words):
            out.append(
                {"id": pid, "servers": servers, **_relevant_skills(app, session_id, pid, words)}
            )
    return out


def _relevant_skills(app: Any, session_id: str, plugin: str, words: list[str]) -> dict[str, Any]:
    """A matching plugin's skills, and the one the search points to (``skill``): the one
    whose id, title or description holds the search, else the plugin's only skill. A
    plugin found by what it does hands over its how-to, not only its name."""

    from clio_agent.gact.offer_search import found_by  # noqa: PLC0415
    from clio_agent.gact.skill_commands import plugin_skills  # noqa: PLC0415

    try:
        refs = plugin_skills(app, session_id, plugin)
    except Exception:  # noqa: BLE001 - the skills are a hint here, never a failure
        return {"skills": []}
    named = [
        ref
        for ref in refs
        if found_by({"id": ref.id, "name": ref.title, "summary": ref.description}, words)
    ]
    chosen = named[0] if named else (refs[0] if len(refs) == 1 else None)
    out: dict[str, Any] = {
        "skills": [{"id": ref.id, "description": summary(ref.description)} for ref in refs]
    }
    if chosen is not None:
        out["skill"] = chosen.id
    return out


def _load_here_skill(app: Any, session_id: str, here: list[dict[str, Any]]) -> dict[str, Any]:
    """Load the skill the first matching plugin here points to, as ``load_skill`` would
    (``{}`` when none, or when it does not load)."""

    from clio_agent.gact.skill_commands import load_skill_for_agent  # noqa: PLC0415

    for row in here:
        if skill_id := row.get("skill"):
            try:
                content = load_skill_for_agent(app, session_id, str(skill_id))
            except Exception as exc:  # noqa: BLE001 - the note still names it to load
                trace.event("TOOLS", "plugin_offer_skill_load_failed skill=%s (%s)", skill_id, exc)
                return {}
            return {"plugin": row["id"], "id": str(skill_id), "content": content}
    return {}


def _here_note(here: list[dict[str, Any]], loaded_skill: Mapping[str, Any] | None = None) -> str:
    """What the agent can use of the matching plugins already here, and any that failed."""

    from clio_agent.tools import mount_failures  # noqa: PLC0415
    from clio_agent.tools.execution import get_active_tool_workspace_root  # noqa: PLC0415

    def named(row: dict[str, Any]) -> str:
        skills = ", ".join(skill["id"] for skill in row.get("skills") or ())
        return f"{row['id']} (skills: {skills})" if skills else str(row["id"])

    ids = "; ".join(named(row) for row in here)
    servers = [server for row in here for server in row["servers"]]
    failed = mount_failures.note(get_active_tool_workspace_root(), servers)
    loaded = (
        f" Its skill {loaded_skill['id']} is loaded below (skill_loaded): follow it."
        if loaded_skill
        else ""
    )
    return (
        f"Already in this conversation, so not offered: {ids}. Its skills load with "
        "load_skill and its tools are in your tool list or found with search_tools."
        + loaded
        + (f" {failed}" if failed else "")
    )


def here_matching(app: Any, session_id: str, query: str) -> tuple[list[str], str]:
    """The plugins already in this conversation a search finds, with the note naming their
    skills (``([], "")`` when none): a tool search that misses still points to them."""

    if not session_id or getattr(getattr(app, "state", None), "sessions", None) is None:
        return [], ""
    here = _here_matching(app, session_id, query)
    return [row["id"] for row in here], (_here_note(here) if here else "")


#: What a search that finds nothing at all says: a plugin is found by what it says it
#: does, so a name only the person uses (a machine, a site, a project) may belong to one
#: that does not name it yet.
MISS_HINT = (
    "A plugin is found by what it says it does: a name only the person uses (a machine, "
    "a site, a project) may be reached by one that does not name it yet, so search for "
    "the kind of thing it is or what it does too."
)


async def _offer_plugin(
    app: Any, session_id: str, record: Any, reason: str, *, required: bool = True
) -> dict[str, Any]:
    from clio_agent.gact import offer_detached  # noqa: PLC0415
    from clio_agent.gact.connector_setup import wait_for_card  # noqa: PLC0415

    here = await asyncio.to_thread(_here, app, session_id)
    if here is not None and record.id in here:
        return await asyncio.to_thread(_joined, app, session_id, record, already=True)
    pending = await asyncio.to_thread(offer_detached.pending_offer, app, session_id, record.id)
    if pending is not None and pending.get("detached"):
        return offer_detached.shown_result(pending, outcome=offer_detached.OFFER_PENDING)
    row = await asyncio.to_thread(_card_row, app, session_id, record, reason)
    trace.event("TOOLS", "plugin_offered plugin=%s session=%s", _ref(record), session_id)
    if not required:
        return await asyncio.to_thread(offer_detached.show, app, row)
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
    from clio_agent.gact.agents.turn_rebuild import (  # noqa: PLC0415
        DEFAULT_APPLY,
        join_running_turn,
    )

    turn = await asyncio.to_thread(
        join_running_turn, app, session_id, str(row.get("apply") or DEFAULT_APPLY)
    )
    result = await asyncio.to_thread(_joined, app, session_id, record, already=False, turn=turn)
    return {**result, "reason": reason}


# -------------------------------------------------------------------- the tool


def _summon_outcome(target: str, result: Mapping[str, Any]) -> dict[str, Any]:
    """An @-named plugin's outcome for the turn's answer: added, or why not."""

    reason = result.get("error") or result.get("refusal")
    return {"error": str(reason)} if reason else {"handle": target}


async def offer_plugin(
    reason: str,
    plugin: str = "",
    search: str = "",
    category: str = "",
    needs: list[str] | None = None,
    required: bool = False,
) -> dict[str, Any]:
    """Offer to add a plugin or connector to this conversation. With ``required`` the
    turn waits for Add or Not now; without it the card stays on screen and the turn goes
    on (:mod:`clio_agent.gact.offer_detached`).
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
    if plugin.strip():  # offered whether or not it is in the list (OL-D3, OL-F6)
        kind, found = await asyncio.to_thread(_match, app, plugin)
        if kind == "connector" and needs:
            from clio_agent.gact.connector_needs_offer import offer_needs  # noqa: PLC0415

            return await offer_needs(found, reason, needs)
        if kind in {"connector", "plugin"}:
            from clio_agent.gact import summon  # noqa: PLC0415

            # A plugin the person @-named: this call is what the summons asked for.
            claimed = summon.claim(summon.PLUGIN, plugin.strip()) or summon.claim(
                summon.PLUGIN, found.id
            )
            if kind == "connector":
                result = await connector_offer.offer_connector(
                    connector=found.id, reason=reason, required=required
                )
            else:
                result = await _offer_plugin(app, session_id, found, reason, required=required)
            summon.started(claimed, _summon_outcome(found.id, result))
            return result
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
        found = offer_search.search_result(
            rows, shown, set(chosen.exclude), offer_listed(), search=search, category=category
        )
        if here := await asyncio.to_thread(_here_matching, app, session_id, search):
            found["in_this_conversation"] = [row["id"] for row in here]
            loaded = await asyncio.to_thread(_load_here_skill, app, session_id, here)
            if loaded:
                found["skill_loaded"] = loaded
            found["message"] += f" {_here_note(here, loaded)}"
        elif search.strip() and not found.get("count"):
            found["message"] += f" {MISS_HINT}"
        return found
    from clio_agent.gact.connector_needs_offer import optional_needs  # noqa: PLC0415

    return {
        **offer_search.overview(rows, len(shown), set(chosen.exclude)),
        # The connectors already here and their empty optional needs (such as API keys),
        # which ``needs`` can ask the person for.
        "optional_needs": await asyncio.to_thread(optional_needs, app, session_id),
    }


def build_offer_plugin_tool() -> Any:
    from clio_agent.gact.offer_list import SETTINGS_PLACE  # noqa: PLC0415
    from clio_agent.gact.plugin_offer_presentation import present, present_start  # noqa: PLC0415

    return native_tool(
        offer_plugin,
        name=TOOL_NAME,
        domain="tasks",
        title="Offer plugin",
        desc=(
            "Offer to add one of the plugins or connectors the person has that this "
            "conversation does not, when the person's request needs or would be better "
            "served by what it brings. The person sees its name and your reason and chooses "
            "Add or Not now; you never add it yourself. The card is how you ask: the person "
            "cannot add a plugin from your text, so a plugin you would suggest is offered "
            "with this call (alongside what you deliver), not as a sentence in your answer. "
            "A connector's card also turns it on and asks for what it needs. With required "
            "true (the request cannot go on without it) the turn waits for the answer: on "
            "Add its tools and skills are yours from your next step and you continue; on "
            "Not now you get a refusal and nothing changed. Otherwise the card stays on "
            "screen and you go on with the work that does not need it, never doing its work "
            "some other way while it waits; if the person adds it later you are "
            "told, and offering it again brings its tools into the turn. The session facts list some "
            "of them and count the rest by category; search (words) or category reaches "
            "every one, the unlisted too, and asks the person nothing: each row says whether "
            "it is in your plugin list. The person chooses that list in "
            f"{SETTINGS_PLACE}; one they took out of it is offered with this call the same "
            "way as any other. With nothing "
            "named it gives the categories and counts, and the optional needs (such as API "
            "keys) of the connectors already here. With needs it asks the person for those "
            "of a connector already here, when a tool's result shows they would help."
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
            "required": {
                "type": "boolean",
                "description": (
                    "True when the request cannot go on without this plugin, so the turn "
                    "waits for the person's answer. False (the default) when you can go on "
                    "without it, for example with another source: the card stays on screen."
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
