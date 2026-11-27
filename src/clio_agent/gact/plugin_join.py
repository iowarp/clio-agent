"""Plugins the person has set up join new conversations by default.

A plugin can declare where its own state says the person set it up (``setup.files``,
:mod:`clio_agent.plugins.setup_state`): the endpoints they added on its page, the
accounts they configured through it. Once they have, a new conversation adds that plugin
to its delta (``add``), as if the person had added it, so its servers, their
instructions and its skills are there from the first message; the person removes it
from the conversation like any other added plugin.

A plugin does not join when the conversation's request already removes or adds it, when
its agent already carries it, when the person left it out of the plugin list in Settings
(:mod:`clio_agent.gact.offer_list`), or when ``plugins.join_set_up`` is off. Only the
library's plugins are candidates (trusted, valid, not following a connector:
:func:`clio_agent.gact.plugin_offer.library`). A join the agent cannot resolve is left
out with a typed trace, never a failed conversation. Nothing here names a plugin.
"""

from __future__ import annotations

from typing import Any

from clio_agent import conf
from clio_agent.runtime import trace

#: Typed trace reasons.
JOINED = "plugin_joined_set_up"
JOIN_UNRESOLVED = "plugin_join_set_up_unresolved"


def join_set_up() -> bool:
    """Whether set-up plugins join new conversations (``plugins.join_set_up``)."""

    return conf.resolve(
        "plugins.join_set_up", env="CLIO_PLUGINS_JOIN_SET_UP", default=True, cast=conf.as_bool
    )


def set_up_refs(app: Any, resolution: Any, delta: Any) -> list[str]:
    """The refs (``marketplace/id``) of the set-up plugins a new conversation adds."""

    from clio_agent.gact.offer_list import entry_key, overlay_or_default  # noqa: PLC0415
    from clio_agent.gact.plugin_offer import library  # noqa: PLC0415
    from clio_agent.plugins.setup_state import is_set_up, setup_files  # noqa: PLC0415

    here = {str(row["id"]) for row in resolution.plugins}
    named = {*delta.add, *delta.remove}
    declaring = [record for record in library(app) if setup_files(record)]
    if not declaring:
        return []
    left_out = overlay_or_default().exclude
    refs: list[str] = []
    for record in declaring:
        ref = f"{record.marketplace}/{record.id}"
        if (
            record.id in here
            or ref in named
            or record.id in named
            or entry_key({"kind": "plugin", "id": record.id, "ref": ref}) in left_out
        ):
            continue
        if is_set_up(record):
            refs.append(ref)
    return refs


def join_set_up_plugins(app: Any, metadata: dict[str, Any], requested: str, resolution: Any) -> Any:
    """Add the set-up plugins to a new conversation's delta in ``metadata`` (in place) and
    return its resolution; ``resolution`` unchanged when none joins."""

    from clio_agent.gact.agents.resolution import (  # noqa: PLC0415
        AGENT_DELTA_KEY,
        SessionAgentUnavailable,
        prepare_session_agent,
    )
    from clio_agent.plugins.resolve import Delta  # noqa: PLC0415

    if not join_set_up():
        return resolution
    raw = dict(metadata.get(AGENT_DELTA_KEY) or {})
    delta = Delta(
        tuple(str(v) for v in raw.get("add") or ()), tuple(str(v) for v in raw.get("remove") or ())
    )
    refs = set_up_refs(app, resolution, delta)
    if not refs:
        return resolution
    before = dict(metadata)
    metadata[AGENT_DELTA_KEY] = {"add": [*delta.add, *refs], "remove": list(delta.remove)}
    try:
        joined = prepare_session_agent(app, metadata, requested)
    except SessionAgentUnavailable as exc:
        metadata.clear()
        metadata.update(before)
        trace.event("TOOLS", "%s plugins=%s (%s)", JOIN_UNRESOLVED, ",".join(refs), exc.reason)
        return resolution
    trace.event("TOOLS", "%s plugins=%s", JOINED, ",".join(refs))
    return joined


__all__ = ["JOINED", "JOIN_UNRESOLVED", "join_set_up", "join_set_up_plugins", "set_up_refs"]
