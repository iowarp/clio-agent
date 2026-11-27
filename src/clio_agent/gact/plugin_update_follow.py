"""A plugin update reaches the conversations open now (host semantics HS3).

A session pins the plugin set it resolved (``plugins-and-composition.md``, "Sessions
are pinned"). When an installed plugin changes under that pin, from any process
(``clio-agent plugin update`` then ``plugin trust``, or the server's own routes), each
open conversation whose pin holds the older copy is pinned again. That is the change
``PATCH /v1/sessions/{sid}/agent`` makes: its servers mount from the new snapshot
(a changed executable part restarts the workspace fleet, drain-aware), its open plugin
tabs watch and read the server mounted now, and the UI gets ``session.updated``. The
model hears of it as a note-mode change, like a connector's
(:mod:`clio_agent.gact.connector_follow`).

A copy that does not resolve yet (an update not yet trusted) leaves the session on
the copy it has; the next store change tries again. A conversation that is not open
is brought up to date when it is next opened. Nothing here names a plugin.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable
from typing import Any, cast

from clio_agent.runtime import trace

logger = logging.getLogger(__name__)

#: How often the plugin store and the open conversations are looked at, in seconds.
FOLLOW_INTERVAL_S = 2.0
PLUGIN_UPDATED = "plugin_updated"


def _copy(row: Any) -> tuple[str, str, str]:
    get = row.get if isinstance(row, dict) else lambda name: getattr(row, name, "")
    return (
        str(get("root") or ""),
        str(get("executable_digest") or ""),
        str(get("text_digest") or ""),
    )


def updated_plugins(resolution: Any, records: Iterable[Any]) -> list[str]:
    """The plugins ``resolution`` pinned whose installed copy has changed since."""

    return [plugin for plugin, _executable in _changes(resolution, records)]


def _changes(resolution: Any, records: Iterable[Any]) -> list[tuple[str, bool]]:
    installed = {(record.marketplace, record.id): record for record in records}
    changes: list[tuple[str, bool]] = []
    for row in resolution.plugins:
        record = installed.get((row.get("marketplace"), row.get("id")))
        if record is not None and _copy(record) != _copy(row):
            changes.append((str(row["id"]), _copy(record)[1] != _copy(row)[1]))
    return changes


def _restart_fleet(
    app: Any, session_id: str, records: list[Any], restarted: set[str] | None
) -> str:
    """Restart the fleets of the session's workspace still running an older plugin copy.

    Each goes through the same drain-aware restart a write-root grant asks for
    (:meth:`~clio_agent.tools.reaper.WorkspaceExecutorReaper.request_restart`): a live
    turn defers it to the turn's end, a conversation that is only on screen does not.
    A workspace without a root folder has fleets too (one per server set), so a rootless
    session is restarted the same way. ``restarted`` collects the fleet keys already
    restarted in this pass.
    """

    from clio_agent.gact.session_fleet import session_fleet_key  # noqa: PLC0415
    from clio_agent.tools.fleet_key import fleet_root  # noqa: PLC0415
    from clio_agent.tools.reaper import (  # noqa: PLC0415
        RESTART_DEFERRED_BUSY,
        RESTART_NO_RESIDENT,
        RESTART_RESTARTED_LIVE,
    )

    agent = getattr(app.state, "agent", None)
    reaper = getattr(agent, "_workspace_reaper", None)
    if reaper is None:
        return RESTART_NO_RESIDENT
    root = fleet_root(session_fleet_key(app, session_id))
    keys = [key for key in stale_fleets(app, records) if fleet_root(key) == root]
    outcomes: set[str] = set()
    for key in keys:
        if restarted is not None:
            if key in restarted:
                outcomes.add("shared")
                continue
            restarted.add(key)
        try:
            outcomes.add(str(reaper.request_restart(key)))
        except Exception:  # noqa: BLE001 - the stale-fleet pass after this one looks again
            logger.exception("plugin update fleet restart failed session=%s", session_id)
            outcomes.add("failed")
    for outcome in (RESTART_DEFERRED_BUSY, "failed", RESTART_RESTARTED_LIVE, "shared"):
        if outcome in outcomes:
            return outcome
    return RESTART_NO_RESIDENT


def _publish_session(app: Any, session_id: str, plugins: list[str]) -> None:
    from clio_agent.gact.events import Event  # noqa: PLC0415
    from clio_agent.gact.types import Session  # noqa: PLC0415

    session = app.state.sessions.get(session_id)
    bus = getattr(app.state, "bus", None)
    if session is None or bus is None:
        return
    payload = Session(**session.to_wire()).model_dump(exclude_none=True)
    bus.publish(
        Event(
            type="session.updated",
            session_id=session_id,
            payload={**payload, "reason": PLUGIN_UPDATED, "plugins": plugins},
        )
    )


def follow_session(
    app: Any, session_id: str, records: list[Any], restarted: set[str] | None = None
) -> list[str]:
    """Pin ``session_id`` again if a plugin it holds was updated; the plugins moved.

    ``restarted`` collects the fleet keys already restarted in this pass, so
    conversations sharing a fleet restart it once.
    """

    from clio_agent.gact import plugin_tabs, session_warmup  # noqa: PLC0415
    from clio_agent.gact.agents import prompt_basis  # noqa: PLC0415
    from clio_agent.gact.agents.resolution import (  # noqa: PLC0415
        AGENT_REF_KEY,
        AGENT_RESOLUTION_KEY,
        SUBAGENT_KEY,
        SessionAgentUnavailable,
        pin_session_agent,
        pinned_resolution,
    )

    session = app.state.sessions.get(session_id)
    metadata = getattr(session, "metadata", None) or {}
    previous = str(metadata.get(AGENT_RESOLUTION_KEY) or "")
    if not previous or metadata.get(SUBAGENT_KEY):
        return []
    try:
        pinned = pinned_resolution(app, previous, str(metadata.get(AGENT_REF_KEY) or ""))
    except SessionAgentUnavailable:
        return []
    changes = _changes(pinned, records)
    updated = [plugin for plugin, _executable in changes]
    if not updated:
        return []
    try:
        resolution = pin_session_agent(app, session_id)
    except SessionAgentUnavailable as exc:
        trace.event(
            "PLUGINS",
            "plugin_update_follow_waiting session=%s plugins=%s reason=%s",
            session_id,
            ",".join(updated),
            exc.reason,
        )
        return []
    if resolution.digest == previous:
        return []
    restarted_now = "unchanged"
    if any(executable for _plugin, executable in changes):
        restarted_now = _restart_fleet(app, session_id, records, restarted)
    prompt_basis.hold_basis(app, session_id, previous)
    session_warmup.start_session_warmup(app, session_id, trigger=PLUGIN_UPDATED)
    plugin_tabs.session_agent_changed(app, session_id)
    _publish_session(app, session_id, updated)
    trace.event(
        "PLUGINS",
        "plugin_update_followed session=%s plugins=%s from=%s to=%s fleet=%s",
        session_id,
        ",".join(updated),
        previous,
        resolution.digest,
        restarted_now,
    )
    return updated


def stale_fleets(app: Any, records: Iterable[Any]) -> list[str]:
    """The resident fleets still running a plugin copy other than the installed one.

    A fleet's stdio servers carry ``PLUGIN_ROOT`` and ``PLUGIN_DATA``; the data folder
    names the plugin. A fleet nobody watches (warmed at start, a child's) is not reached
    by :func:`follow_session`, yet its server shares that plugin's data with the current
    copy, so it is restarted too, and its next start takes the installed copy
    (:mod:`clio_agent.plugins.installed_servers`).
    """

    from pathlib import Path  # noqa: PLC0415

    from clio_agent.plugins.service import plugin_store_root  # noqa: PLC0415

    agent = getattr(getattr(app, "state", None), "agent", None)
    state = getattr(agent, "_workspace_state", None)
    if not callable(state):
        return []
    installed: dict[str, set[str]] = {}
    for record in records:
        installed.setdefault(str(record.id), set()).add(str(record.root))
    data_root = plugin_store_root() / "data"
    lock, executors, _leases = cast(tuple[Any, dict[str, Any], Any], state())
    with lock:
        fleets = list(executors.items())
    stale: list[str] = []
    for key, executor in fleets:
        for spec in (getattr(executor, "_clio_namespace_specs", None) or {}).values():
            env = getattr(spec, "env", None) or {}
            root, data = str(env.get("PLUGIN_ROOT") or ""), str(env.get("PLUGIN_DATA") or "")
            if not root or not data:
                continue
            try:
                plugin = Path(data).relative_to(data_root).as_posix()
            except ValueError:
                continue
            if installed.get(plugin) and root not in installed[plugin]:
                stale.append(str(key))
                break
    return stale


def _restart_stale_fleets(app: Any, store: Any, restarted: set[str] | None = None) -> None:
    from clio_agent.tools.reaper import restart_every_fleet_at  # noqa: PLC0415

    agent = getattr(getattr(app, "state", None), "agent", None)
    reaper = getattr(agent, "_workspace_reaper", None)
    if reaper is None:
        return
    for key in stale_fleets(app, store.installed_records()):
        if restarted is not None and key in restarted:
            continue
        try:
            outcome = restart_every_fleet_at(agent, reaper, key)
        except Exception:  # noqa: BLE001 - the next store change looks again
            logger.exception("plugin update stale fleet restart failed fleet=%s", key)
            continue
        trace.event("PLUGINS", "plugin_update_stale_fleet fleet=%s outcome=%s", key, outcome)


def follow_open_sessions(app: Any, last: Any = None) -> tuple[Any, dict[str, list[str]]]:
    """Bring each open conversation onto the plugins installed now.

    Looks again only when the plugin store or the set of open conversations changed
    since ``last`` (the first value returned). Returns that key and, per session moved,
    the plugins whose update reached it. When the store changed, a resident fleet still
    running an older copy restarts as well (:func:`stale_fleets`).
    """

    from clio_agent.gact.agents.resolution import plugin_service  # noqa: PLC0415
    from clio_agent.gact.connector_follow import open_session_ids  # noqa: PLC0415

    store = plugin_service(app).store
    sessions = open_session_ids(app)
    key = (store.revision(), tuple(sessions))
    if key == last:
        return key, {}
    store_changed = not isinstance(last, tuple) or last[:1] != key[:1]
    if not sessions:
        if store_changed:
            _restart_stale_fleets(app, store)
        return key, {}
    records = store.records()
    moved: dict[str, list[str]] = {}
    restarted: set[str] = set()
    for session_id in sessions:
        try:
            updated = follow_session(app, session_id, records, restarted)
        except Exception:  # noqa: BLE001 - one session's failure must not stop the others
            logger.exception("plugin update follow failed session=%s", session_id)
            continue
        if updated:
            moved[session_id] = updated
    if store_changed:
        _restart_stale_fleets(app, store, restarted)
    return key, moved


async def _run(app: Any) -> None:
    from clio_agent.gact.off_loop import run_off_loop  # noqa: PLC0415

    last: Any = None
    while True:
        try:
            last, _moved = await run_off_loop(follow_open_sessions, app, last)
        except Exception:  # noqa: BLE001 - the loop outlives one failed pass
            logger.exception("plugin update follow failed")
        await asyncio.sleep(FOLLOW_INTERVAL_S)


def start(app: Any) -> asyncio.Task[None] | None:
    """Start following plugin updates on the running loop (an app with plugins only)."""

    if getattr(app.state, "plugin_service", None) is None:
        return None
    task = asyncio.create_task(_run(app), name="clio-plugin-update-follow")
    app.state.plugin_update_follow_task = task
    return task


__all__ = [
    "FOLLOW_INTERVAL_S",
    "follow_open_sessions",
    "follow_session",
    "start",
    "updated_plugins",
]
