"""A conversation follows the person's connectors (CN9, CN10).

A session pins its agent's resolution, and the plugins that follow enabled
connectors are part of it (``PluginService.resolve``). The pin records which
connectors it was resolved with (``agent_connectors``). When the person enables or
disables one, :func:`follow_connectors` pins the session again and keeps the
model's prompt on what it had: the change reaches the agent as the existing
``plugin_change`` injection (``plugin-change-delivery.md``, the ``note`` mode),
whose added tools run at once, and the plugin-change notice shows it. A connector
change sweeps the conversations that are open now (:func:`follow_in_open_sessions`);
any other conversation follows the first time its resolution is read.
"""

from __future__ import annotations

from typing import Any

from clio_agent.runtime import trace


def follow_connectors(app: Any, session_id: str) -> Any:
    """Pin ``session_id`` again with the person's connectors, as a note-mode change."""

    from clio_agent.gact import session_warmup  # noqa: PLC0415
    from clio_agent.gact.agents import prompt_basis  # noqa: PLC0415
    from clio_agent.gact.agents.resolution import (  # noqa: PLC0415
        AGENT_RESOLUTION_KEY,
        pin_session_agent,
    )

    session = app.state.sessions.get(session_id)
    previous = str(((getattr(session, "metadata", None) or {}).get(AGENT_RESOLUTION_KEY)) or "")
    resolution = pin_session_agent(app, session_id)
    prompt_basis.hold_basis(app, session_id, previous)
    trace.event(
        "PLUGINS",
        "connector_follow_repinned session=%s from=%s to=%s",
        session_id,
        previous,
        resolution.digest,
    )
    session_warmup.start_session_warmup(app, session_id, trigger="connector_changed")
    return resolution


def open_session_ids(app: Any) -> list[str]:
    """Sessions someone is watching or a turn is running in."""

    bus = getattr(app.state, "bus", None)
    watched = set(getattr(bus, "_subs", {}) or {}) if bus is not None else set()
    running = {
        str(sid) for sid, task in (getattr(app.state, "in_flight_turns", {}) or {}).items() if task
    }
    return sorted(sid for sid in watched | running if sid and app.state.sessions.get(sid))


def follow_in_open_sessions(app: Any) -> list[str]:
    """Bring every open conversation up to date; return the ones that changed."""

    from clio_agent.gact.agents.resolution import (  # noqa: PLC0415
        AGENT_RESOLUTION_KEY,
        SessionAgentUnavailable,
        session_resolution,
    )

    changed: list[str] = []
    for sid in open_session_ids(app):
        before = str((app.state.sessions.get(sid).metadata or {}).get(AGENT_RESOLUTION_KEY) or "")
        try:
            session_resolution(app, sid)
        except SessionAgentUnavailable as exc:
            trace.event("PLUGINS", "connector_follow_failed session=%s reason=%s", sid, exc.reason)
            continue
        after = str((app.state.sessions.get(sid).metadata or {}).get(AGENT_RESOLUTION_KEY) or "")
        if after != before:
            changed.append(sid)
    return changed
