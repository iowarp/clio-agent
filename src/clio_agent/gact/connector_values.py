"""A conversation's own connector values: read, write, apply (CV-D1 to CV-D5).

Values the agent sets with ``set_connector_value`` and values the person types in a
setup card inside a conversation belong to that conversation (the overlay,
:mod:`clio_agent.connectors.overlay`), kept in its session metadata. The person's
defaults in ``connectors.yaml`` stay what Extensions edits; a card's "Save as
default" also writes them there. Secrets and sign-ins are never per conversation.

Applying a conversation's values never touches another conversation's server:

* the conversation's server set is resolved again with the new overlay, so its
  connector declarations, and the fleet key, carry the new values
  (``plugins.servers``, ``tools.fleet_key``); the next resolve for this conversation
  gets its own fleet, built lazily, and every other conversation keeps its own;
* :func:`move_session_fleet` builds that fleet now, lists the connector's tools
  there (its state), and, when a turn of this conversation is running, moves the
  connector's namespace of that turn to it (``LiveToolRefresher.move``) so the
  agent's next step calls the server with the new values.

A fleet left without conversations is reaped on the normal idle path.
"""

from __future__ import annotations

import asyncio
import dataclasses
import threading
from collections.abc import Collection, Mapping
from typing import Any

from clio_agent.connectors import overlay, state
from clio_agent.connectors.entries import Entry
from clio_agent.runtime import trace

#: Typed reason: the session's fleet variant could not be resolved (plain fleet used).
VARIANT_UNRESOLVED = "connector_values_variant_unresolved"

_LOCK = threading.Lock()


def _session(app: Any, sid: str) -> Any:
    sessions = getattr(getattr(app, "state", None), "sessions", None)
    return sessions.get(sid) if sessions is not None and sid else None


def session_overlay(app: Any, sid: str) -> overlay.Overlay:
    """The conversation's checked connector values (``{}`` when it has none)."""

    session = _session(app, sid)
    metadata = getattr(session, "metadata", None) or {}
    raw = metadata.get(overlay.KEY) if isinstance(metadata, Mapping) else None
    if not raw:
        return {}
    return overlay.validated(raw, state.all_entries(), where=sid)


def session_fleet_variant(app: Any, sid: str) -> str:
    """The connector-values variant of the fleet serving ``sid`` ("" for the plain one)."""

    if not session_overlay(app, sid):
        return ""
    from clio_agent.gact.agents.resolution import session_server_set  # noqa: PLC0415
    from clio_agent.plugins.servers import server_set_variant  # noqa: PLC0415

    try:
        return server_set_variant(session_server_set(app, sid))
    except Exception as exc:  # noqa: BLE001 - typed; the caller reads the plain fleet
        trace.event("TOOLS", "%s session=%s (%s)", VARIANT_UNRESOLVED, sid, type(exc).__name__)
        return ""


def session_choice(app: Any, sid: str, found: Entry) -> state.Choice:
    """The person's choice for ``found`` with this conversation's values over it."""

    choice = state.choice(found.id)
    own = session_overlay(app, sid).get(found.id) if sid else None
    if not own:
        return choice
    return dataclasses.replace(choice, values={**choice.values, **own})


def write_overlay(
    app: Any, sid: str, connector_id: str, changes: Mapping[str, str | None]
) -> dict[str, str]:
    """Apply ``changes`` (checked values; ``None`` clears) to the conversation's overlay."""

    sessions = getattr(getattr(app, "state", None), "sessions", None)
    with _LOCK:
        current = session_overlay(app, sid)
        mine = overlay.merged(current.get(connector_id, {}), changes)
        if mine:
            current[connector_id] = mine
        else:
            current.pop(connector_id, None)
        sessions.update(sid, metadata_patch={overlay.KEY: current})
    trace.event(
        "TOOLS",
        "connector_values_conversation session=%s connector=%s keys=%s",
        sid,
        connector_id,
        ",".join(sorted(changes)) or "-",
    )
    return mine


def session_row(app: Any, sid: str, found: Entry) -> dict[str, Any]:
    """The connector's wire row as this conversation sees it (CV-D5).

    ``needs[].value`` is the value in use here; ``needs[].default_value`` the person's
    default; ``needs[].conversation`` whether this conversation set its own.
    """

    from clio_agent.gact import connectors_service as service  # noqa: PLC0415

    own = session_overlay(app, sid).get(found.id, {})
    defaults = state.choice(found.id)
    row = service.row(app, found, session_choice(app, sid, found))
    for need in row["needs"]:
        if need["kind"] in overlay.OVERLAY_KINDS:
            need["default_value"] = defaults.values.get(need["key"])
            need["conversation"] = need["key"] in own
    row["session_id"] = sid
    return row


# ----------------------------------------------------------------- applying


def move_session_fleet(app: Any, sid: str, found: Entry, *, move_turn: bool) -> dict[str, Any]:
    """Build ``sid``'s fleet for its current values and list the connector there.

    Returns ``{status, tool_count, reason}`` (``status`` ``ready``, ``unreachable`` or
    ``not_mounted`` when this conversation's agent does not mount the connector).
    With ``move_turn``, the running turn of this conversation (the caller's context)
    takes the connector's tools from the new fleet from its next step.
    """

    from clio_agent.gact import connectors_service as service  # noqa: PLC0415
    from clio_agent.gact.runtime.globals import (  # noqa: PLC0415
        _gact_app_context,
        _tool_session_context,
    )
    from clio_agent.tools.mcp_discovery import ensure_namespace  # noqa: PLC0415

    agent = getattr(app.state, "agent", None)
    resolve = getattr(agent, "_active_tool_executor", None)
    if not callable(resolve):
        return {"status": "not_mounted", "tool_count": None, "reason": None}
    with _gact_app_context(app), _tool_session_context(sid, lease_fleet=False):
        executor = resolve()
    spec = (getattr(executor, "_clio_namespace_specs", None) or {}).get(found.id)
    if spec is None:
        trace.event("TOOLS", "connector_values_not_mounted session=%s connector=%s", sid, found.id)
        return {"status": "not_mounted", "tool_count": None, "reason": None}
    try:
        tools = ensure_namespace(found.id, spec)
    except Exception as exc:  # noqa: BLE001 - forwards: the unreachable state's reason
        declared = {"command": spec.command, "url": spec.url}
        reason = service.unreachable_reason(found, declared, f"{type(exc).__name__}: {exc}")
        trace.event("TOOLS", "connector_unreachable connector=%s session=%s", found.id, sid)
        return {"status": "unreachable", "tool_count": None, "reason": reason}
    merge = getattr(executor, "merge_namespace_tools", None)
    if callable(merge):
        merge(found.id, tools)
    if move_turn:
        move_running_turn(found.id, executor)
    return {"status": "ready", "tool_count": len(tools), "reason": None}


def move_running_turn(namespace: str, executor: Any) -> bool:
    """Have the turn running in this context take ``namespace`` from ``executor``."""

    from clio_agent.gact.agents.clio_react import active_loop  # noqa: PLC0415
    from clio_agent.gact.agents.live_tool_refresh import LiveToolRefresher  # noqa: PLC0415

    program = getattr(active_loop(), "agent", None)
    refresher = getattr(program, "_clio_tool_refresh", None)
    if refresher is None or not hasattr(refresher, "inner"):
        trace.event("TOOLS", "connector_values_no_turn namespace=%s", namespace)
        return False
    if refresher.inner is None:
        refresher.inner = LiveToolRefresher(executor, set())
    refresher.inner.move(namespace, executor)
    return True


def default_keys(metadata: Mapping[str, Any] | None, plain: Mapping[str, Any]) -> dict:
    """The values a card's ``metadata.save_as_default`` keys make the person's defaults."""

    from clio_agent.gact import connectors_service as service  # noqa: PLC0415

    keys = (metadata or {}).get("save_as_default") or []
    if not isinstance(keys, list) or not all(isinstance(key, str) for key in keys):
        raise service.ConnectorError(
            "connector_default_invalid", "save_as_default must be a list of need keys"
        )
    unknown = sorted(set(keys) - set(plain))
    if unknown:
        raise service.ConnectorError(
            "connector_default_unknown", f"Save as default names no value given: {unknown}"
        )
    return {key: plain[key] for key in keys}


def session_fleet(app: Any, sid: str) -> Any:
    """The fleet ``sid`` resolves to now (built lazily; nothing is started)."""

    from clio_agent.gact.runtime.globals import (  # noqa: PLC0415
        _gact_app_context,
        _tool_session_context,
    )

    resolve = getattr(getattr(app.state, "agent", None), "_active_tool_executor", None)
    if not callable(resolve):
        return None
    with _gact_app_context(app), _tool_session_context(sid, lease_fleet=False):
        return resolve()


async def set_session_values(
    app: Any,
    sid: str,
    connector_id: str,
    values: Mapping[str, Any],
    *,
    by_agent: bool,
    save_as_default: Collection[str] = (),
    move_turn: bool = False,
) -> dict[str, Any]:
    """Set needs for this conversation, or, for keys in ``save_as_default``, as defaults.

    A value saved as default is the person's for every conversation that has not set
    its own, this one included (its own value for that key is cleared). A secret goes
    to the credential store for every conversation (CN8). Returns the conversation's
    row, with ``state`` from the conversation's own fleet and ``moved``: whether this
    conversation's values changed, so it is served by another fleet from now on.
    """

    from clio_agent.gact import connectors_service as service  # noqa: PLC0415

    found = service.entry(connector_id)
    plain, secret = service.check_values(found, values, by_agent=by_agent)
    unknown = sorted(set(save_as_default) - set(plain))
    if unknown:
        raise service.ConnectorError(
            "connector_default_unknown", f"Save as default names no value given: {unknown}"
        )
    before = session_overlay(app, sid).get(found.id, {})
    own: dict[str, str | None] = {
        key: (None if key in save_as_default else value) for key, value in plain.items()
    }
    if secret:
        await asyncio.to_thread(service.secrets.save, found.id, secret)
    after = before
    if own:
        after = await asyncio.to_thread(write_overlay, app, sid, found.id, own)
    defaults = {key: plain[key] for key in save_as_default}
    if defaults:
        await asyncio.to_thread(state.update, found.id, values=defaults)
    if secret or defaults:
        await service.apply(app, found)  # every fleet that follows the defaults
    moved = after != before
    status: dict[str, Any] = {"status": "ready", "tool_count": None, "reason": None}
    if moved and state.choice(found.id).enabled:
        status = await asyncio.to_thread(move_session_fleet, app, sid, found, move_turn=move_turn)
    row = await asyncio.to_thread(session_row, app, sid, found)
    if row["state"]["status"] == "ready" and status["status"] == "unreachable":
        row["state"] = {"status": "unreachable", "needs": [], "reason": status["reason"]}
    if status["status"] == "ready" and status["tool_count"] is not None:
        row["tool_count"] = status["tool_count"]
    row["moved"] = moved
    service.publish_changed(app, found.id, [sid])
    return row
