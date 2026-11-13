"""A connector's plugin in a conversation (CN9, CN10 *Amended*).

Enabling a connector adds nothing to any conversation. The person adds its plugin to
one conversation (the session delta: the plugin picker, or the discovery card through
:func:`add_to_conversation`), and the change reaches the agent as the existing
``plugin_change`` injection (``plugin-change-delivery.md``, the ``note`` mode), whose
added tools run at once.

A conversation whose delta names the plugin of a connector the person then turns off
drops that plugin with a typed ``removed`` row (``connectors.follow.drop_off``); turning
it on again brings it back. :func:`follow_connectors` pins such a session again, and a
connector change brings the open conversations up to date
(:func:`follow_in_open_sessions`): only those whose pin no longer matches are pinned
again. Any other conversation follows the first time its resolution is read.
"""

from __future__ import annotations

from typing import Any

from clio_agent.runtime import trace


class ConnectorAddError(RuntimeError):
    """The connector's plugin could not join the conversation; ``reason`` is typed."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


def follow_connectors(app: Any, session_id: str) -> Any:
    """Pin ``session_id`` again with the connectors as they are, as a note-mode change."""

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


def in_conversation(app: Any, session_id: str, connector_id: str) -> list[str]:
    """The plugins of ``connector_id`` this conversation's resolution has."""

    from clio_agent.gact.agents.resolution import session_resolution  # noqa: PLC0415

    resolution = session_resolution(app, session_id)
    return [row["id"] for row in resolution.plugins if row.get("follows") == connector_id]


def add_to_conversation(app: Any, session_id: str, connector_id: str) -> list[str]:
    """Add the connector's plugin to this conversation's delta, as a note-mode change.

    The same change ``PATCH /v1/sessions/{sid}/agent`` makes with ``apply: note``.
    Returns the plugin ids now in the conversation for the connector.
    """

    from clio_agent.connectors.follow import plugin_refs  # noqa: PLC0415
    from clio_agent.gact import session_warmup  # noqa: PLC0415
    from clio_agent.gact.agents import prompt_basis  # noqa: PLC0415
    from clio_agent.gact.agents.resolution import (  # noqa: PLC0415
        AGENT_RESOLUTION_KEY,
        SessionAgentUnavailable,
        pin_session_agent,
        plugin_service,
        session_delta,
    )
    from clio_agent.plugins.resolve import Delta  # noqa: PLC0415

    if present := in_conversation(app, session_id, connector_id):
        return present
    refs = plugin_refs(connector_id, plugin_service(app).store.records())
    session = app.state.sessions.get(session_id)
    previous = str(((getattr(session, "metadata", None) or {}).get(AGENT_RESOLUTION_KEY)) or "")
    delta = session_delta(app, session_id).then(Delta(tuple(refs)))
    try:
        resolution = pin_session_agent(app, session_id, delta=delta)
    except SessionAgentUnavailable as exc:
        raise ConnectorAddError(exc.reason, str(exc)) from exc
    prompt_basis.hold_basis(app, session_id, previous)
    session_warmup.start_session_warmup(app, session_id, trigger="agent_changed")
    trace.event(
        "PLUGINS",
        "connector_added_to_conversation connector=%s session=%s plugins=%s",
        connector_id,
        session_id,
        ",".join(refs),
    )
    return [row["id"] for row in resolution.plugins if row.get("follows") == connector_id]


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
